package com.evolveum.polygon.owncloud;

import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.identityconnectors.common.security.GuardedString;
import org.identityconnectors.framework.common.exceptions.AlreadyExistsException;
import org.identityconnectors.framework.common.exceptions.ConnectorException;
import org.identityconnectors.framework.common.objects.AttributeBuilder;
import org.identityconnectors.framework.common.objects.AttributeDelta;
import org.identityconnectors.framework.common.objects.AttributeDeltaBuilder;
import org.identityconnectors.framework.common.objects.AttributeUtil;
import org.identityconnectors.framework.common.objects.ConnectorObject;
import org.identityconnectors.framework.common.objects.Name;
import org.identityconnectors.framework.common.objects.ObjectClass;
import org.identityconnectors.framework.common.objects.Uid;
import org.identityconnectors.framework.common.objects.filter.FilterBuilder;
import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilderFactory;
import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;

/** Local-only HTTP contract coverage for ownCloud OCS discovery and create. */
public final class OwnCloudCreateContractTest {
    private static final String UID = "employee + A";
    private static final ObjectClass USER = new ObjectClass("User");

    private OwnCloudCreateContractTest() {}

    public static void main(String[] args) throws Exception {
        var createResponseCode = new AtomicInteger(100);
        var updateResponseCode = new AtomicInteger(100);
        var getHttpStatus = new AtomicInteger(200);
        var listResponse = new AtomicReference<>(ocs(100, "{\"users\":[\"employee + A\"]}"));
        var detailResponse = new AtomicReference<>(ocs(100,
                "{\"id\":\"employee + A\",\"displayname\":\"Example Employee\","
                        + "\"email\":\"employee@example.com\",\"enabled\":\"true\","
                        + "\"password\":\"private-value\",\"secret\":\"private-value\",\"roles\":[\"admin\"]}"));
        var requestCount = new AtomicInteger();
        var requestPath = new AtomicReference<String>();
        var requestMethod = new AtomicReference<String>();
        var listPath = new AtomicReference<String>();
        var detailPath = new AtomicReference<String>();
        var requestProtocol = new AtomicReference<String>();
        var requestContentType = new AtomicReference<String>();
        var requestOcsHeader = new AtomicReference<String>();
        var requestAuthorization = new AtomicReference<String>();
        var requestBody = new AtomicReference<String>();

        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/ocs/v1.php/cloud", exchange -> {
            requestCount.incrementAndGet();
            requestPath.set(exchange.getRequestURI().toString());
            requestMethod.set(exchange.getRequestMethod());
            if ("GET".equals(exchange.getRequestMethod())) {
                if (exchange.getRequestURI().getPath().endsWith("/users")) listPath.set(requestPath.get());
                else detailPath.set(requestPath.get());
            }
            requestProtocol.set(exchange.getProtocol());
            requestContentType.set(exchange.getRequestHeaders().getFirst("Content-Type"));
            requestOcsHeader.set(exchange.getRequestHeaders().getFirst("OCS-APIRequest"));
            requestAuthorization.set(exchange.getRequestHeaders().getFirst("Authorization"));
            requestBody.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));

            String json;
            int httpStatus;
            if ("PUT".equals(exchange.getRequestMethod())) {
                json = ocs(updateResponseCode.get(), "{\"id\":\"employee + A\"}");
                httpStatus = 200;
            } else if ("POST".equals(exchange.getRequestMethod())) {
                json = ocs(createResponseCode.get(), "[]");
                httpStatus = 200;
            } else if (exchange.getRequestURI().getPath().endsWith("/users")) {
                json = listResponse.get();
                httpStatus = getHttpStatus.get();
            } else {
                json = detailResponse.get();
                httpStatus = getHttpStatus.get();
            }

            byte[] response = json.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(httpStatus, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
        });
        server.start();

        OwnCloudConnector connector = new OwnCloudConnector();
        try {
            testResourceMappings();
            OwnCloudConfiguration configuration = new OwnCloudConfiguration();
            configuration.setBaseAddress("http://127.0.0.1:" + server.getAddress().getPort() + "/ocs/v1.php/cloud");
            configuration.setTimeoutSeconds(5);
            configuration.setRestUsername("api-user");
            configuration.setRestPassword(new GuardedString("service-only-secret".toCharArray()));
            connector.init(configuration);

            testListAndDetail(connector, requestCount, listPath, detailPath, requestOcsHeader, requestAuthorization);
            testLegacyDetailWithoutId(connector, detailResponse);
            testMismatchedReturnedIdFails(connector, detailResponse);
            testMissingUser(connector, detailResponse, detailPath);
            testMalformedUserFails(connector, detailResponse);
            testMalformedEnabledFails(connector, detailResponse);
            testMalformedListFails(connector, listResponse);
            testAuthorizationFailureFails(connector, getHttpStatus);
            testCreate(connector, createResponseCode, requestCount, requestPath, requestProtocol,
                    requestContentType, requestOcsHeader, requestAuthorization, requestBody);
            testUpdate(connector, updateResponseCode, requestCount, requestPath, requestMethod,
                    requestContentType, requestOcsHeader, requestAuthorization, requestBody, detailResponse);
            System.out.println("OwnCloudCreateContractTest passed");
        } finally {
            connector.dispose();
            server.stop(0);
        }
    }

    private static void testListAndDetail(OwnCloudConnector connector, AtomicInteger requestCount,
            AtomicReference<String> listPath, AtomicReference<String> detailPath, AtomicReference<String> ocsHeader,
            AtomicReference<String> authorization) {
        var listed = new ArrayList<ConnectorObject>();
        connector.executeQuery(USER, null, object -> { listed.add(object); return true; }, null);
        assertEquals(1, listed.size(), "list yields one account");
        ConnectorObject account = listed.getFirst();
        assertEquals(UID, account.getUid().getUidValue(), "list preserves native UID");
        assertEquals("/ocs/v1.php/cloud/users?format=json", listPath.get(), "list endpoint");
        assertEquals("true", ocsHeader.get(), "OCS request header");
        assertServiceAuthorization(authorization.get());

        int beforeDetail = requestCount.get();
        var detail = new ArrayList<ConnectorObject>();
        connector.executeQuery(USER, FilterBuilder.equalTo(new Uid(UID)), object -> { detail.add(object); return true; }, null);
        assertEquals(1, detail.size(), "get yields one account");
        account = detail.getFirst();
        assertEquals(UID, account.getUid().getUidValue(), "get preserves native UID");
        assertEquals(UID, account.getName().getNameValue(), "account name is exposed through ConnId NAME separately from UID");
        assertEquals("/ocs/v1.php/cloud/users/employee%20%2B%20A?format=json", detailPath.get(), "encoded UID detail endpoint");
        assertEquals(beforeDetail + 1, requestCount.get(), "get makes one detail request");
        assertEquals("true", account.getAttributeByName("enabled").getValue().getFirst().toString(), "string enabled value becomes Boolean true");
        assertEquals(Boolean.class, account.getAttributeByName("enabled").getValue().getFirst().getClass(), "enabled is Boolean typed");
        assertEquals("Example Employee", account.getAttributeByName("displayname").getValue().getFirst(), "display name mapped");
        assertEquals("employee@example.com", account.getAttributeByName("email").getValue().getFirst(), "email mapped");
        if (account.getAttributeByName("password") != null || account.getAttributeByName("secret") != null
                || account.getAttributeByName("roles") != null) {
            throw new AssertionError("private ownCloud fields must not be projected");
        }
    }

    private static void testMissingUser(OwnCloudConnector connector, AtomicReference<String> detailResponse,
            AtomicReference<String> detailPath) {
        detailResponse.set(ocs(101, "[]"));
        var missing = new ArrayList<ConnectorObject>();
        connector.executeQuery(USER, FilterBuilder.equalTo(new Uid("absent-user")), object -> { missing.add(object); return true; }, null);
        assertEquals(0, missing.size(), "OCS user-not-found status returns no result");
        assertEquals("/ocs/v1.php/cloud/users/absent-user?format=json", detailPath.get(), "missing user queried by UID");
    }

    private static void testLegacyDetailWithoutId(OwnCloudConnector connector, AtomicReference<String> detailResponse) {
        detailResponse.set(ocs(100, "{\"displayname\":\"Legacy User\",\"email\":\"legacy@example.com\",\"enabled\":true}"));
        var result = new ArrayList<ConnectorObject>();
        connector.executeQuery(USER, FilterBuilder.equalTo(new Uid(UID)), object -> { result.add(object); return true; }, null);
        assertEquals(1, result.size(), "legacy ownCloud details without an id use the requested UID");
        assertEquals(UID, result.getFirst().getUid().getUidValue(), "legacy detail keeps the requested UID");
        detailResponse.set(ocs(100, "{\"id\":\"employee + A\",\"displayname\":\"Example Employee\","
                + "\"email\":\"employee@example.com\",\"enabled\":\"true\","
                + "\"password\":\"private-value\",\"secret\":\"private-value\",\"roles\":[\"admin\"]}"));
    }

    private static void testMismatchedReturnedIdFails(OwnCloudConnector connector, AtomicReference<String> detailResponse) {
        detailResponse.set(ocs(100, "{\"id\":\"different-user\",\"displayname\":\"Example Employee\"}"));
        expectConnectorFailure(() -> connector.executeQuery(USER, FilterBuilder.equalTo(new Uid(UID)), object -> true, null),
                "a returned provider ID must match the requested UID");
        detailResponse.set(ocs(100, "{\"id\":\"employee + A\",\"displayname\":\"Example Employee\","
                + "\"email\":\"employee@example.com\",\"enabled\":\"true\","
                + "\"password\":\"private-value\",\"secret\":\"private-value\",\"roles\":[\"admin\"]}"));
    }

    private static void testMalformedUserFails(OwnCloudConnector connector, AtomicReference<String> detailResponse) {
        detailResponse.set(ocs(100, "null"));
        expectConnectorFailure(() -> connector.executeQuery(USER, FilterBuilder.equalTo(new Uid("bad-response")), object -> true, null),
                "malformed user data must fail explicitly");
    }

    private static void testMalformedEnabledFails(OwnCloudConnector connector, AtomicReference<String> detailResponse) {
        detailResponse.set(ocs(100, "{\"id\":\"employee + A\",\"enabled\":\"yes\"}"));
        expectConnectorFailure(() -> connector.executeQuery(USER, FilterBuilder.equalTo(new Uid(UID)), object -> true, null),
                "malformed enabled value must not be converted to false");
        detailResponse.set(ocs(100, "{\"id\":\"employee + A\",\"displayname\":\"Example Employee\","
                + "\"email\":\"employee@example.com\",\"enabled\":\"true\","
                + "\"password\":\"private-value\",\"secret\":\"private-value\",\"roles\":[\"admin\"]}"));
    }

    private static void testMalformedListFails(OwnCloudConnector connector, AtomicReference<String> listResponse) {
        listResponse.set(ocs(100, "{}"));
        expectConnectorFailure(() -> connector.executeQuery(USER, null, object -> true, null),
                "missing users array must fail explicitly");
        listResponse.set(ocs(100, "{\"users\":[{\"id\":\"employee + A\"}]}"));
        expectConnectorFailure(() -> connector.executeQuery(USER, null, object -> true, null),
                "non-string user ID entries must fail explicitly");
        listResponse.set(ocs(100, "{\"users\":[\"employee + A\"]}"));
    }

    private static void testAuthorizationFailureFails(OwnCloudConnector connector, AtomicInteger httpStatus) {
        httpStatus.set(403);
        expectConnectorFailure(() -> connector.executeQuery(USER, FilterBuilder.equalTo(new Uid("protected-user")), object -> true, null),
                "HTTP authorization failure must propagate");
        httpStatus.set(200);
    }

    private static void testCreate(OwnCloudConnector connector, AtomicInteger responseCode, AtomicInteger requestCount,
            AtomicReference<String> requestPath, AtomicReference<String> protocol,
            AtomicReference<String> contentType, AtomicReference<String> ocsHeader,
            AtomicReference<String> authorization, AtomicReference<String> body) {
        Set<org.identityconnectors.framework.common.objects.Attribute> attributes = createAttributes();
        assertEquals("user+test@example.com", AttributeUtil.getStringValue(AttributeUtil.find(Name.NAME, attributes)),
                "create input is ConnId NAME");
        if (AttributeUtil.find(Uid.NAME, attributes) != null) {
            throw new AssertionError("create input must not send read-only ConnId UID");
        }
        var createdUid = connector.create(USER, attributes, null);
        assertEquals("user+test@example.com", createdUid.getUidValue(), "create returns the requested native UID");
        assertEquals("/ocs/v1.php/cloud/users?format=json", requestPath.get(), "OCS create path");
        assertEquals("HTTP/1.1", protocol.get(), "HTTP protocol");
        assertEquals("application/x-www-form-urlencoded", contentType.get(), "form content type");
        assertEquals("true", ocsHeader.get(), "OCS API request header");
        assertEquals("userid=user%2Btest%40example.com&password=new+pass%2F%26value", body.get(), "encoded create fields");
        assertServiceAuthorization(authorization.get());

        responseCode.set(102);
        try {
            connector.create(USER, createAttributes(), null);
            throw new AssertionError("duplicate OCS status must not be treated as successful creation");
        } catch (AlreadyExistsException expected) {
            assertEquals("user+test@example.com", expected.getUid().getUidValue(), "duplicate exception identifies the existing UID");
        }
        assertEquals(13, requestCount.get(), "each explicit operation makes only its expected request");
    }

    private static void testUpdate(OwnCloudConnector connector, AtomicInteger responseCode, AtomicInteger requestCount,
            AtomicReference<String> requestPath, AtomicReference<String> requestMethod,
            AtomicReference<String> contentType, AtomicReference<String> ocsHeader,
            AtomicReference<String> authorization, AtomicReference<String> body,
            AtomicReference<String> detailResponse) {
        Set<AttributeDelta> attributes = Set.of(
                replace("displayname", "Updated Employee"),
                replace("email", "updated@example.com"));
        int before = requestCount.get();
        Set<AttributeDelta> updated = connector.updateDelta(USER, new Uid(UID), attributes, null);
        assertEquals(Set.of(), updated, "ownCloud update returns no follow-up deltas");
        assertEquals(Set.of(), updated, "ownCloud update returns no follow-up deltas");
        assertEquals(before + 2, requestCount.get(), "ownCloud uses one OCS request per identity attribute");
        assertEquals("PUT", requestMethod.get(), "ownCloud identity update uses OCS PUT");
        assertEquals("/ocs/v1.php/cloud/users/employee%20%2B%20A?format=json", requestPath.get(),
                "ownCloud update targets the URL-encoded immutable UID");
        assertEquals("application/x-www-form-urlencoded", contentType.get(), "ownCloud update form content type");
        assertEquals("true", ocsHeader.get(), "ownCloud update OCS request header");
        assertServiceAuthorization(authorization.get());
        String lastBody = body.get();
        if (!lastBody.equals("key=display&value=Updated+Employee")
                && !lastBody.equals("key=email&value=updated%40example.com")) {
            throw new AssertionError("ownCloud update sends exactly one supported field with form encoding: " + lastBody);
        }
        if (lastBody.contains("password") || lastBody.contains("uid") || lastBody.contains("_id")) {
            throw new AssertionError("ownCloud update must not send password or immutable account IDs");
        }

        detailResponse.set(ocs(100, "{\"id\":\"employee + A\",\"displayname\":\"Updated Employee\","
                + "\"email\":\"updated@example.com\"}"));
        var readback = new ArrayList<ConnectorObject>();
        connector.executeQuery(USER, FilterBuilder.equalTo(new Uid(UID)), object -> { readback.add(object); return true; }, null);
        assertEquals(1, readback.size(), "ownCloud identity update is readable by immutable UID");
        ConnectorObject account = readback.getFirst();
        assertEquals(UID, account.getUid().getUidValue(), "ownCloud readback preserves UID");
        assertEquals("Updated Employee", account.getAttributeByName("displayname").getValue().getFirst(),
                "ownCloud display-name readback");
        assertEquals("updated@example.com", account.getAttributeByName("email").getValue().getFirst(),
                "ownCloud email readback");
        if (account.getAttributeByName("password") != null || account.getAttributeByName("secret") != null) {
            throw new AssertionError("ownCloud readback must not expose credentials or provider secrets");
        }

        int beforeRepeated = requestCount.get();
        assertEquals(Set.of(), connector.updateDelta(USER, new Uid(UID), attributes, null),
                "repeated ownCloud identity update is idempotent");
        assertEquals(beforeRepeated + 2, requestCount.get(), "repeated ownCloud updates remain bounded to identity fields");

        int beforeRejected = requestCount.get();
        expectConnectorFailure(() -> connector.updateDelta(USER, new Uid(UID),
                Set.of(replace("password", "must-not-send")), null),
                "ownCloud must reject credential updates");
        assertEquals(beforeRejected, requestCount.get(), "unsupported ownCloud update is rejected before any provider call");

        responseCode.set(102);
        expectConnectorFailure(() -> connector.updateDelta(USER, new Uid(UID),
                Set.of(replace("displayname", "Invalid Update")), null),
                "OCS invalid update result must not be reported as success");
        responseCode.set(100);
        detailResponse.set(ocs(100, "{\"id\":\"employee + A\",\"displayname\":\"Example Employee\","
                + "\"email\":\"employee@example.com\",\"enabled\":\"true\","
                + "\"password\":\"private-value\",\"secret\":\"private-value\",\"roles\":[\"admin\"]}"));
    }

    private static AttributeDelta replace(String name, Object value) {
        return new AttributeDeltaBuilder().setName(name).addValueToReplace(value).build();
    }

    private static Set<org.identityconnectors.framework.common.objects.Attribute> createAttributes() {
        return Set.of(
                AttributeBuilder.build(Name.NAME, "user+test@example.com"),
                AttributeBuilder.build(AttributeUtil.createSpecialName("PASSWORD"), new GuardedString("new pass/&value".toCharArray())));
    }

    private static void testResourceMappings() throws Exception {
        Path resourceXml = Path.of(System.getProperty("owncloud.resourceXml"));
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        factory.setNamespaceAware(true);
        factory.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
        factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        factory.setFeature("http://xml.org/sax/features/external-general-entities", false);
        factory.setFeature("http://xml.org/sax/features/external-parameter-entities", false);
        Element resource = factory.newDocumentBuilder().parse(resourceXml.toFile()).getDocumentElement();
        String commonNs = "http://midpoint.evolveum.com/xml/ns/public/common/common-3";
        String uidOutboundPath = outboundPath(resource, commonNs, "ri:uid");
        assertEquals("$focus/name", uidOutboundPath, "resource maps account name to writable native uid");
        if (findAttribute(resource, commonNs, "ri:_id") != null) {
            throw new AssertionError("resource must not send the read-only provider UID outbound");
        }
    }

    private static Element findAttribute(Element resource, String commonNs, String refValue) {
        NodeList attributes = resource.getElementsByTagNameNS(commonNs, "attribute");
        for (int i = 0; i < attributes.getLength(); i++) {
            Element attribute = (Element) attributes.item(i);
            if (refValue.equals(text(attribute, commonNs, "ref"))) return attribute;
        }
        return null;
    }

    private static String outboundPath(Element resource, String commonNs, String refValue) {
        return text(findAttribute(resource, commonNs, refValue), commonNs, "outbound", "source", "path");
    }

    private static String text(Element element, String namespace, String... childNames) {
        Node current = element;
        for (String childName : childNames) {
            if (!(current instanceof Element currentElement)) return null;
            NodeList children = currentElement.getElementsByTagNameNS(namespace, childName);
            if (children.getLength() == 0) return null;
            current = children.item(0);
        }
        return current.getTextContent().trim();
    }

    private static void assertServiceAuthorization(String actual) {
        String expected = "Basic " + Base64.getEncoder().encodeToString(
                "api-user:service-only-secret".getBytes(StandardCharsets.UTF_8));
        assertEquals(expected, actual, "service credentials authenticate the request");
    }

    private static String ocs(int status, String data) {
        return "{\"ocs\":{\"meta\":{\"status\":\"ok\",\"statuscode\":" + status + "},\"data\":" + data + "}}";
    }

    private static void expectConnectorFailure(Runnable operation, String contract) {
        try {
            operation.run();
            throw new AssertionError(contract);
        } catch (ConnectorException expected) {
            // Expected provider failure remains visible at the ConnId boundary.
        }
    }

    private static void assertEquals(Object expected, Object actual, String contract) {
        if (!expected.equals(actual)) {
            throw new AssertionError(contract + " did not match the expected contract (expected " + expected + ", got " + actual + ")");
        }
    }
}
