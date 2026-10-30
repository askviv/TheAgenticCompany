import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.atomic.AtomicReference;
import org.identityconnectors.common.security.GuardedString;
import org.identityconnectors.framework.api.ConnectorFacade;
import org.identityconnectors.framework.api.ConnectorFacadeFactory;
import org.identityconnectors.framework.api.ConnectorInfo;
import org.identityconnectors.framework.api.ConnectorInfoManagerFactory;
import org.identityconnectors.framework.api.APIConfiguration;
import org.identityconnectors.framework.api.ConnectorKey;
import org.identityconnectors.framework.common.objects.Attribute;
import org.identityconnectors.framework.common.objects.AttributeBuilder;
import org.identityconnectors.framework.common.objects.AttributeDelta;
import org.identityconnectors.framework.common.objects.AttributeDeltaBuilder;
import org.identityconnectors.framework.common.objects.Name;
import org.identityconnectors.framework.common.objects.ObjectClass;
import org.identityconnectors.framework.common.objects.OperationalAttributes;
import org.identityconnectors.framework.common.objects.Uid;
import org.identityconnectors.framework.common.objects.ConnectorObject;
import org.identityconnectors.framework.common.objects.filter.FilterBuilder;

public final class ConnectorCreateContractTest {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final String TEST_PASSWORD = "Local-Dummy-Password-42";

    private record MockApi(HttpServer server, AtomicReference<String> path, AtomicReference<String> method,
                           AtomicReference<String> body, AtomicReference<String> auth) implements AutoCloseable {
        @Override
        public void close() {
            server.stop(0);
        }

        static MockApi start(int status, String response) throws Exception {
            var path = new AtomicReference<String>();
            var method = new AtomicReference<String>();
            var body = new AtomicReference<String>();
            var auth = new AtomicReference<String>();
            HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.createContext("/", exchange -> {
                path.set(exchange.getRequestURI().getPath());
                method.set(exchange.getRequestMethod());
                body.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
                String authorization = exchange.getRequestHeaders().getFirst("Authorization");
                auth.set(authorization == null ? exchange.getRequestHeaders().getFirst("X-Auth-Token") : authorization);
                byte[] bytes = response.getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().set("Content-Type", "application/json");
                exchange.sendResponseHeaders(status, bytes.length);
                exchange.getResponseBody().write(bytes);
                exchange.close();
            });
            server.start();
            return new MockApi(server, path, method, body, auth);
        }

        String base(String context) {
            return "http://127.0.0.1:" + server.getAddress().getPort() + context;
        }
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 2) throw new IllegalArgumentException("Expected Rocket.Chat and Mailu bundle paths");
        testRocketChatCreate(Path.of(args[0]));
        testRocketChatUpdate(Path.of(args[0]));
        testMailuCreate(Path.of(args[1]));
        testMailuUpdate(Path.of(args[1]));
        System.out.println("Rocket.Chat and Mailu create/update contracts passed");
    }

    private static void testRocketChatCreate(Path bundle) throws Exception {
        try (MockApi api = MockApi.start(200,
                "{\"success\":true,\"user\":{\"_id\":\"mock-rocket-uid\",\"username\":\"newuser\"}}")) {
            ConnectorFacade facade = facade(bundle, "com.evolveum.polygon.scimrest.connector-rocketchat",
                    "com.evolveum.polygon.rocketchat.RocketChatConnector", api.base("/api/v1"),
                    "/users.list?count=1", "mock-token", "mock-operator-id");
            try {
                Set<Attribute> attributes = new HashSet<>();
                attributes.add(new Name("newuser"));
                attributes.add(AttributeBuilder.build("name", "New User"));
                attributes.add(AttributeBuilder.build("email", "newuser@example.com"));
                attributes.add(AttributeBuilder.build(OperationalAttributes.PASSWORD_NAME,
                        new GuardedString(TEST_PASSWORD.toCharArray())));
                attributes.add(AttributeBuilder.build("roles", "user"));

                Uid uid = facade.create(new ObjectClass("User"), attributes, null);
                JsonNode payload = JSON.readTree(api.body().get());
                require("mock-rocket-uid".equals(uid.getUidValue()), "Rocket.Chat response UID wrapper");
                require("/api/v1/users.create".equals(api.path().get()), "Rocket.Chat create endpoint");
                require("mock-token".equals(api.auth().get()), "Rocket.Chat auth header");
                require("newuser".equals(payload.path("username").asText()), "Rocket.Chat username payload");
                require("New User".equals(payload.path("name").asText()), "Rocket.Chat display name payload");
                require("newuser@example.com".equals(payload.path("email").asText()), "Rocket.Chat email payload");
                require("user".equals(payload.path("roles").get(0).asText()), "Rocket.Chat role payload");
                require(TEST_PASSWORD.equals(payload.path("password").asText()), "Rocket.Chat guarded password serialization");
                require(!payload.has("_id") && !payload.has("uid"), "Rocket.Chat create request omits immutable UID");
            } finally {
                facade.dispose();
            }
        }
    }

    private static void testMailuCreate(Path bundle) throws Exception {
        try (MockApi api = MockApi.start(201,
                "{\"email\":\"newuser@example.com\",\"displayed_name\":\"New User\"}")) {
            ConnectorFacade facade = facade(bundle, "com.evolveum.polygon.scimrest.connector-mailu",
                    "com.evolveum.polygon.mailu.MailuConnector", api.base("/api/v1"),
                    "/user", "mock-token", null);
            try {
                Set<Attribute> attributes = new HashSet<>();
                // Mailbox email is the ConnId UID. Create uses only NAME; the
                // connector maps it to Mailu's email field without passing UID.
                attributes.add(new Name("newuser@example.com"));
                attributes.add(AttributeBuilder.build("displayed_name", "New User"));
                attributes.add(AttributeBuilder.build(OperationalAttributes.PASSWORD_NAME,
                        new GuardedString(TEST_PASSWORD.toCharArray())));
                attributes.add(AttributeBuilder.build("enabled", true));
                attributes.add(AttributeBuilder.build("enable_imap", true));
                attributes.add(AttributeBuilder.build("enable_pop", false));
                attributes.add(AttributeBuilder.build("allow_spoofing", false));

                Uid uid = facade.create(new ObjectClass("User"), attributes, null);
                JsonNode payload = JSON.readTree(api.body().get());
                require("newuser@example.com".equals(uid.getUidValue()), "Mailu immutable email UID");
                require("/api/v1/user".equals(api.path().get()), "Mailu create endpoint");
                require("Bearer mock-token".equals(api.auth().get()), "Mailu auth header");
                require("newuser@example.com".equals(payload.path("email").asText()), "Mailu email from ConnId NAME");
                require("New User".equals(payload.path("displayed_name").asText()), "Mailu displayed name payload");
                require(TEST_PASSWORD.equals(payload.path("raw_password").asText()), "Mailu guarded password serialization");
                require(!payload.has("uid") && !payload.has("username"), "Mailu API request omits ConnId identity aliases");
                require(payload.path("enabled").asBoolean(), "Mailu enabled default");
                require(payload.path("enable_imap").asBoolean(), "Mailu IMAP default");
                require(!payload.path("enable_pop").asBoolean(), "Mailu POP default");
            } finally {
                facade.dispose();
            }
        }
    }

    private static void testRocketChatUpdate(Path bundle) throws Exception {
        String nativeId = "immutable-rocket-user-id";
        try (MockApi api = MockApi.start(200,
                "{\"success\":true,\"user\":{\"_id\":\"" + nativeId + "\",\"username\":\"keep-user\","
                        + "\"name\":\"Updated Name\",\"emails\":[{\"address\":\"updated@example.com\"}],\"roles\":[\"user\"]}}")) {
            ConnectorFacade facade = facade(bundle, "com.evolveum.polygon.scimrest.connector-rocketchat",
                    "com.evolveum.polygon.rocketchat.RocketChatConnector", api.base("/api/v1"),
                    "/users.list?count=1", "mock-token", "mock-operator-id");
            try {
                Set<AttributeDelta> identity = Set.of(
                        replace("name", "Updated Name"), replace("email", "updated@example.com"));
                Set<AttributeDelta> updated = facade.updateDelta(new ObjectClass("User"), new Uid(nativeId), identity, null);
                require(updated.isEmpty(), "Rocket.Chat update returns no follow-up deltas");
                require(java.util.Objects.equals("POST", api.method().get()), "Rocket.Chat update uses POST");
                require(java.util.Objects.equals("/api/v1/users.update", api.path().get()), "Rocket.Chat identity update endpoint");
                require(java.util.Objects.equals("mock-token", api.auth().get()), "Rocket.Chat update uses connector auth");
                JsonNode request = JSON.readTree(api.body().get());
                require(nativeId.equals(request.path("userId").asText()), "Rocket.Chat update targets immutable user ID");
                require("Updated Name".equals(request.path("data").path("name").asText()), "Rocket.Chat display-name update");
                require("updated@example.com".equals(request.path("data").path("email").asText()), "Rocket.Chat email update");
                require(request.path("data").size() == 2, "Rocket.Chat sends only the two agreed identity attributes");
                require(!request.path("data").has("username") && !request.path("data").has("roles")
                        && !request.path("data").has("password") && !request.has("_id"),
                        "Rocket.Chat update omits username, roles, password, and immutable ID");

                require(facade.updateDelta(new ObjectClass("User"), new Uid(nativeId), identity, null).isEmpty(),
                        "repeated Rocket.Chat update is idempotent");
                require(nativeId.equals(JSON.readTree(api.body().get()).path("userId").asText()),
                        "repeated Rocket.Chat update retains immutable ID");
            } finally {
                facade.dispose();
            }
        }
    }

    private static void testMailuUpdate(Path bundle) throws Exception {
        String email = "new+user@example.com";
        try (MockApi api = MockApi.start(200,
                "{\"email\":\"" + email + "\",\"displayed_name\":\"Updated Name\"}")) {
            ConnectorFacade facade = facade(bundle, "com.evolveum.polygon.scimrest.connector-mailu",
                    "com.evolveum.polygon.mailu.MailuConnector", api.base("/api/v1"),
                    "/user", "mock-token", null);
            try {
                Set<AttributeDelta> displayedName = Set.of(replace("displayed_name", "Updated Name"));
                require(facade.updateDelta(new ObjectClass("User"), new Uid(email), displayedName, null).isEmpty(),
                        "Mailu update returns no follow-up deltas");
                require(java.util.Objects.equals("PATCH", api.method().get()), "Mailu identity update uses PATCH");
                require(java.util.Objects.equals("/api/v1/user/new+user@example.com", api.path().get()), "Mailu update uses encoded mailbox path");
                require(java.util.Objects.equals("Bearer mock-token", api.auth().get()), "Mailu update uses connector auth");
                JsonNode request = JSON.readTree(api.body().get());
                require("Updated Name".equals(request.path("displayed_name").asText()), "Mailu display-name update");
                require(request.size() == 1 && !request.has("raw_password") && !request.has("email"),
                        "Mailu update sends only display name, never password or mailbox address");

                require(facade.updateDelta(new ObjectClass("User"), new Uid(email), displayedName, null).isEmpty(),
                        "repeated Mailu update is idempotent");
                JsonNode repeated = JSON.readTree(api.body().get());
                require(repeated.size() == 1 && "Updated Name".equals(repeated.path("displayed_name").asText()),
                        "repeated Mailu update remains display-name-only");
            } finally {
                facade.dispose();
            }
        }
    }

    private static ConnectorFacade facade(Path jar, String bundleName, String connectorClass,
                                          String base, String testEndpoint, String token, String rocketUserId)
            throws Exception {
        var manager = ConnectorInfoManagerFactory.getInstance().getLocalManager(jar.toUri().toURL());
        ConnectorInfo info = manager.findConnectorInfo(new ConnectorKey(bundleName, "0.2.0-tac.1", connectorClass));
        if (info == null) throw new IllegalStateException("Connector bundle was not discovered");
        APIConfiguration api = info.createDefaultAPIConfiguration();
        var properties = api.getConfigurationProperties();
        properties.setPropertyValue("baseAddress", base);
        properties.setPropertyValue("restTestEndpoint", testEndpoint);
        properties.setPropertyValue("timeoutSeconds", 10);
        properties.setPropertyValue("restTokenValue", new GuardedString(token.toCharArray()));
        if (rocketUserId != null) properties.setPropertyValue("rocketChatUserId", rocketUserId);
        return ConnectorFacadeFactory.getInstance().newInstance(api);
    }

    private static void require(boolean condition, String contract) {
        if (!condition) throw new AssertionError(contract);
    }

    private static AttributeDelta replace(String name, Object value) {
        return new AttributeDeltaBuilder().setName(name).addValueToReplace(value).build();
    }
}
