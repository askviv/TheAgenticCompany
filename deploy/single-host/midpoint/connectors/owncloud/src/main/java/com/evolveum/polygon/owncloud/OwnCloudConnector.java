package com.evolveum.polygon.owncloud;

import com.evolveum.polygon.scimrest.groovy.impl.ManifestBasedConnector;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import org.identityconnectors.common.security.GuardedString;
import org.identityconnectors.framework.common.exceptions.AlreadyExistsException;
import org.identityconnectors.framework.common.exceptions.ConnectorException;
import org.identityconnectors.framework.common.objects.Attribute;
import org.identityconnectors.framework.common.objects.AttributeDelta;
import org.identityconnectors.framework.common.objects.AttributeUtil;
import org.identityconnectors.framework.common.objects.Name;
import org.identityconnectors.framework.common.objects.ObjectClass;
import org.identityconnectors.framework.common.objects.OperationOptions;
import org.identityconnectors.framework.common.objects.ResultsHandler;
import org.identityconnectors.framework.common.objects.Uid;
import org.identityconnectors.framework.common.objects.ConnectorObjectBuilder;
import org.identityconnectors.framework.common.objects.AttributeBuilder;
import org.identityconnectors.framework.common.objects.filter.EqualsFilter;
import org.identityconnectors.framework.common.objects.filter.Filter;
import org.identityconnectors.framework.spi.ConnectorClass;
import org.identityconnectors.framework.spi.operations.SearchOp;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

@ConnectorClass(displayNameKey = "owncloud.rest.display", configurationClass = OwnCloudConfiguration.class, messageCatalogPaths = "Messages")
public class OwnCloudConnector extends ManifestBasedConnector {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final HttpClient HTTP = HttpClient.newBuilder()
            .version(HttpClient.Version.HTTP_1_1)
            .followRedirects(HttpClient.Redirect.NEVER)
            .build();

    /**
     * The pinned SCIMREST generic search mapper fails to carry OCS user IDs into
     * ConnId's mandatory Uid. Keep this provider-specific OCS projection small
     * and explicit while leaving authentication, configuration, schema, and
     * create support on the native connector contract.
     */
    @Override
    public void executeQuery(ObjectClass objectClass, Filter filter, ResultsHandler handler, OperationOptions options) {
        if (!new ObjectClass("User").equals(objectClass)) {
            super.executeQuery(objectClass, filter, handler, options);
            return;
        }

        String requestedUid = null;
        if (filter != null) {
            if (!(filter instanceof EqualsFilter equals)) {
                throw new UnsupportedOperationException("ownCloud supports only UID equality searches");
            }
            String filterName = equals.getName();
            if (!Uid.NAME.equals(filterName) && !Name.NAME.equals(filterName)
                    && !"_id".equals(filterName) && !"uid".equals(filterName) && !"id".equals(filterName)) {
                throw new UnsupportedOperationException("ownCloud supports only UID equality searches");
            }
            requestedUid = AttributeUtil.getStringValue(equals.getAttribute());
            if (requestedUid == null || requestedUid.isBlank()) {
                throw new IllegalArgumentException("ownCloud UID search requires a non-empty value");
            }
        }

        OwnCloudConfiguration configuration = (OwnCloudConfiguration) getConfiguration();
        String baseAddress = configuration.getBaseAddress().replaceAll("/+$", "");
        try {
            if (requestedUid != null) {
                JsonNode user = readUser(configuration, baseAddress, requestedUid);
                if (user != null && !handler.handle(toConnectorObject(user, requestedUid))) return;
                return;
            }

            JsonNode users = readUserIds(configuration, baseAddress);
            for (JsonNode entry : users) {
                if (!entry.isTextual() || entry.asText().isBlank()) {
                    throw new ConnectorException("ownCloud OCS users.list contains a malformed user identifier");
                }
                String uid = entry.asText();
                JsonNode user = readUser(configuration, baseAddress, uid);
                if (user != null && !handler.handle(toConnectorObject(user, uid))) return;
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new ConnectorException("ownCloud user search was interrupted", e);
        } catch (java.io.IOException e) {
            throw new ConnectorException("ownCloud user search request failed", e);
        }
    }

    private JsonNode readUserIds(OwnCloudConfiguration configuration, String baseAddress)
            throws java.io.IOException, InterruptedException {
        JsonNode root = sendOcs(configuration, URI.create(baseAddress + "/users?format=json"), false);
        JsonNode users = root.path("ocs").path("data").path("users");
        if (!users.isArray()) throw new ConnectorException("ownCloud OCS users.list response has no users array");
        return users;
    }

    private JsonNode readUser(OwnCloudConfiguration configuration, String baseAddress, String uid)
            throws java.io.IOException, InterruptedException {
        String encodedUid = URLEncoder.encode(uid, StandardCharsets.UTF_8).replace("+", "%20");
        JsonNode root = sendOcs(configuration, URI.create(baseAddress + "/users/" + encodedUid + "?format=json"), true);
        if (root == null) return null;
        JsonNode user = root.path("ocs").path("data");
        if (user.isMissingNode() || user.isNull() || !user.isObject()) {
            throw new ConnectorException("ownCloud OCS users.info response has no user object");
        }
        JsonNode returnedUid = user.get("id");
        if (returnedUid != null && !returnedUid.isNull()
                && (!returnedUid.isTextual() || !uid.equals(returnedUid.asText()))) {
            throw new ConnectorException("ownCloud OCS users.info response has an invalid user identifier");
        }
        return user;
    }

    private JsonNode sendOcs(OwnCloudConfiguration configuration, URI uri, boolean allowNotFound)
            throws java.io.IOException, InterruptedException {
        HttpRequest request = HttpRequest.newBuilder(uri)
                .timeout(Duration.ofSeconds(configuration.getTimeoutSeconds()))
                .version(HttpClient.Version.HTTP_1_1)
                .header("OCS-APIRequest", "true")
                .header("Accept", "application/json")
                .header("Authorization", basicAuthorization(configuration.getRestUsername(), configuration.getRestPassword()))
                .GET()
                .build();
        HttpResponse<String> response = HTTP.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw new ConnectorException("ownCloud OCS user search returned HTTP status " + response.statusCode());
        }
        JsonNode root = JSON.readTree(response.body());
        JsonNode meta = root.path("ocs").path("meta");
        int status = meta.path("statuscode").asInt(-1);
        if (allowNotFound && status == 101) return null;
        if (status != 100) throw new ConnectorException("ownCloud OCS user search returned status " + status);
        return root;
    }

    private JsonNode updateUserAttribute(OwnCloudConfiguration configuration, URI uri, String field, String value)
            throws java.io.IOException, InterruptedException {
        String form = "key=" + URLEncoder.encode(field, StandardCharsets.UTF_8)
                + "&value=" + URLEncoder.encode(value, StandardCharsets.UTF_8);
        HttpRequest request = HttpRequest.newBuilder(uri)
                .timeout(Duration.ofSeconds(configuration.getTimeoutSeconds()))
                .version(HttpClient.Version.HTTP_1_1)
                .header("OCS-APIRequest", "true")
                .header("Accept", "application/json")
                .header("Content-Type", "application/x-www-form-urlencoded")
                .header("Authorization", basicAuthorization(configuration.getRestUsername(), configuration.getRestPassword()))
                .PUT(HttpRequest.BodyPublishers.ofString(form, StandardCharsets.UTF_8))
                .build();
        HttpResponse<String> response = HTTP.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw new ConnectorException("ownCloud OCS user update returned HTTP status " + response.statusCode());
        }
        JsonNode root = JSON.readTree(response.body());
        JsonNode meta = root.path("ocs").path("meta");
        int status = meta.path("statuscode").asInt(-1);
        if (status != 100) {
            throw new ConnectorException("ownCloud OCS user update returned status " + status);
        }
        return root.path("ocs").path("data");
    }

    /** Update only ownCloud identity fields supported by OCS. */
    @Override
    public Set<AttributeDelta> updateDelta(ObjectClass objectClass, Uid uid,
            Set<AttributeDelta> attributes, OperationOptions options) {
        if (!new ObjectClass("User").equals(objectClass)) {
            throw new UnsupportedOperationException("ownCloud only supports updating user identity fields");
        }
        if (uid == null || uid.getUidValue() == null || uid.getUidValue().isBlank()) {
            throw new ConnectorException("ownCloud user update requires an immutable UID");
        }
        if (attributes.isEmpty()) return Set.of();

        String encodedUid = URLEncoder.encode(uid.getUidValue(), StandardCharsets.UTF_8).replace("+", "%20");
        URI uri = URI.create(((OwnCloudConfiguration) getConfiguration()).getBaseAddress().replaceAll("/+$", "")
                + "/users/" + encodedUid + "?format=json");
        OwnCloudConfiguration configuration = (OwnCloudConfiguration) getConfiguration();
        Map<String, String> updates = new java.util.TreeMap<>();
        for (AttributeDelta attribute : attributes) {
            String name = attribute.getName();
            final String ocsField;
            if ("displayname".equals(name)) ocsField = "display";
            else if ("email".equals(name)) ocsField = "email";
            else throw new ConnectorException("ownCloud user update does not support attribute " + name);

            if ((attribute.getValuesToAdd() != null && !attribute.getValuesToAdd().isEmpty())
                    || (attribute.getValuesToRemove() != null && !attribute.getValuesToRemove().isEmpty())
                    || attribute.getValuesToReplace() == null || attribute.getValuesToReplace().size() != 1) {
                throw new ConnectorException("ownCloud user update requires one replacement value for " + name);
            }
            Object rawValue = attribute.getValuesToReplace().getFirst();
            if (!(rawValue instanceof String value) || value.isBlank()) {
                throw new ConnectorException("ownCloud user update requires a non-empty " + name);
            }
            updates.put(name, value);
        }
        for (Map.Entry<String, String> update : updates.entrySet()) {
            String name = update.getKey();
            String ocsField = "displayname".equals(name) ? "display" : "email";
            String value = update.getValue();
            try {
                JsonNode updated = updateUserAttribute(configuration, uri, ocsField, value);
                JsonNode returnedUid = updated == null ? null : updated.get("id");
                if (returnedUid != null && !returnedUid.isNull()
                        && (!returnedUid.isTextual() || !uid.getUidValue().equals(returnedUid.asText()))) {
                    throw new ConnectorException("ownCloud OCS user update response did not match the requested UID");
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new ConnectorException("ownCloud user update was interrupted while changing " + name, e);
            } catch (tools.jackson.core.JacksonException e) {
                throw new ConnectorException("ownCloud OCS user update response was not valid JSON for " + name, e);
            } catch (java.io.IOException e) {
                throw new ConnectorException("ownCloud user update request failed while changing " + name, e);
            }
        }
        return Set.of();
    }

    private static org.identityconnectors.framework.common.objects.ConnectorObject toConnectorObject(JsonNode user, String uid) {
        ConnectorObjectBuilder builder = new ConnectorObjectBuilder()
                .setObjectClass(new ObjectClass("User"))
                .setUid(new Uid(uid))
                .setName(new Name(uid));
        addText(builder, user, "displayname");
        addText(builder, user, "email");
        JsonNode enabled = user.get("enabled");
        if (enabled != null && !enabled.isNull()) {
            if (enabled.isBoolean()) {
                builder.addAttribute(AttributeBuilder.build("enabled", enabled.asBoolean()));
            } else if (enabled.isTextual()
                    && ("true".equalsIgnoreCase(enabled.asText()) || "false".equalsIgnoreCase(enabled.asText()))) {
                builder.addAttribute(AttributeBuilder.build("enabled", Boolean.parseBoolean(enabled.asText())));
            } else {
                throw new ConnectorException("ownCloud OCS users.info response has an invalid enabled value");
            }
        }
        return builder.build();
    }

    private static void addText(ConnectorObjectBuilder builder, JsonNode user, String name) {
        JsonNode value = user.get(name);
        if (value != null && !value.isNull()) builder.addAttribute(AttributeBuilder.build(name, value.asText()));
    }

    private static String basicAuthorization(String username, GuardedString password) {
        java.util.concurrent.atomic.AtomicReference<String> value = new java.util.concurrent.atomic.AtomicReference<>();
        password.access(chars -> value.set(basicAuthorization(username, chars)));
        return value.get();
    }

    /**
     * ownCloud OCS accepts provisioning credentials as form fields. The pinned
     * SCIMREST create serializer only emits JSON, so this one operation uses a
     * small JDK HTTP/1.1 request with separate guarded service and user passwords.
     */
    @Override
    public Uid create(ObjectClass objectClass, Set<Attribute> attributes, OperationOptions options) {
        if (!new ObjectClass("User").equals(objectClass)) {
            throw new UnsupportedOperationException("ownCloud only supports creating user accounts");
        }

        Attribute uidAttribute = AttributeUtil.find("uid", attributes);
        if (uidAttribute == null) uidAttribute = AttributeUtil.find(Name.NAME, attributes);
        String uid = AttributeUtil.getStringValue(uidAttribute);
        GuardedString newPassword = AttributeUtil.getPasswordValue(attributes);
        if (uid == null || uid.isBlank() || newPassword == null) {
            throw new ConnectorException("ownCloud account creation requires a user ID and guarded password");
        }

        OwnCloudConfiguration configuration = (OwnCloudConfiguration) getConfiguration();
        String baseAddress = configuration.getBaseAddress().replaceAll("/+$", "");
        URI uri = URI.create(baseAddress + "/users?format=json");
        String encodedUid = URLEncoder.encode(uid, StandardCharsets.UTF_8);

        try {
            java.util.concurrent.atomic.AtomicReference<HttpResponse<String>> result = new java.util.concurrent.atomic.AtomicReference<>();
            newPassword.access(userPassword -> configuration.getRestPassword().access(apiPassword -> {
                String form = "userid=" + encodedUid + "&password="
                        + URLEncoder.encode(new String(userPassword), StandardCharsets.UTF_8);
                String authorization = basicAuthorization(configuration.getRestUsername(), apiPassword);
                HttpRequest request = HttpRequest.newBuilder(uri)
                        .timeout(Duration.ofSeconds(configuration.getTimeoutSeconds()))
                        .version(HttpClient.Version.HTTP_1_1)
                        .header("OCS-APIRequest", "true")
                        .header("Accept", "application/json")
                        .header("Content-Type", "application/x-www-form-urlencoded")
                        .header("Authorization", authorization)
                        .POST(HttpRequest.BodyPublishers.ofString(form, StandardCharsets.UTF_8))
                        .build();
                try {
                    result.set(HTTP.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8)));
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    throw new ConnectorException("ownCloud account creation was interrupted", e);
                } catch (java.io.IOException e) {
                    throw new ConnectorException("ownCloud account creation request failed", e);
                }
            }));

            HttpResponse<String> response = result.get();
            if (response.statusCode() < 200 || response.statusCode() >= 300) {
                throw new ConnectorException("ownCloud account creation returned HTTP status " + response.statusCode());
            }
            JsonNode meta = JSON.readTree(response.body()).path("ocs").path("meta");
            int statusCode = meta.path("statuscode").asInt(-1);
            if (statusCode == 102) {
                throw new AlreadyExistsException("ownCloud account already exists").initUid(new Uid(uid));
            }
            if (statusCode != 100) {
                throw new ConnectorException("ownCloud account creation returned OCS status " + statusCode);
            }
            return new Uid(uid);
        } catch (tools.jackson.core.JacksonException e) {
            throw new ConnectorException("ownCloud account creation response was not valid JSON", e);
        }
    }

    private static String basicAuthorization(String username, char[] password) {
        byte[] user = username.getBytes(StandardCharsets.UTF_8);
        java.nio.ByteBuffer secretBuffer = StandardCharsets.UTF_8.encode(java.nio.CharBuffer.wrap(password));
        byte[] secret = new byte[secretBuffer.remaining()];
        secretBuffer.get(secret);
        int secretLength = secret.length;
        byte[] credentials = new byte[user.length + 1 + secretLength];
        System.arraycopy(user, 0, credentials, 0, user.length);
        credentials[user.length] = (byte) ':';
        System.arraycopy(secret, 0, credentials, user.length + 1, secretLength);
        java.util.Arrays.fill(secret, (byte) 0);
        String encoded = Base64.getEncoder().encodeToString(credentials);
        java.util.Arrays.fill(credentials, (byte) 0);
        return "Basic " + encoded;
    }
}
