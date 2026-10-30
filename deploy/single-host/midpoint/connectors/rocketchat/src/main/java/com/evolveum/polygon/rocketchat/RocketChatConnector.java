package com.evolveum.polygon.rocketchat;

import com.evolveum.polygon.scimrest.groovy.impl.ManifestBasedConnector;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.identityconnectors.common.security.GuardedString;
import org.identityconnectors.framework.common.exceptions.ConnectorException;
import org.identityconnectors.framework.common.objects.Attribute;
import org.identityconnectors.framework.common.objects.AttributeDelta;
import org.identityconnectors.framework.common.objects.AttributeUtil;
import org.identityconnectors.framework.common.objects.Name;
import org.identityconnectors.framework.common.objects.ObjectClass;
import org.identityconnectors.framework.common.objects.OperationOptions;
import org.identityconnectors.framework.common.objects.Attribute;
import org.identityconnectors.framework.common.objects.AttributeUtil;
import org.identityconnectors.framework.common.objects.Uid;
import org.identityconnectors.framework.spi.ConnectorClass;

@ConnectorClass(displayNameKey = "rocketchat.rest.display", configurationClass = RocketChatConfiguration.class, messageCatalogPaths = "Messages")
public class RocketChatConnector extends ManifestBasedConnector {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final HttpClient HTTP = HttpClient.newBuilder()
            .version(HttpClient.Version.HTTP_1_1)
            .followRedirects(HttpClient.Redirect.NEVER)
            .build();

    /**
     * Rocket.Chat wraps the created account in a `user` object. The pinned
     * SCIMREST create response handler expects the account object at the top
     * level and cannot extract this provider response shape. Keep request
     * serialization and the response projection explicit at this boundary.
     */
    @Override
    public Uid create(ObjectClass objectClass, Set<Attribute> attributes, OperationOptions options) {
        if (!new ObjectClass("User").equals(objectClass)) {
            throw new UnsupportedOperationException("Rocket.Chat only supports creating user accounts");
        }
        if (AttributeUtil.getUidAttribute(attributes) != null) {
            throw new ConnectorException("Rocket.Chat user creation must not include an immutable UID");
        }

        Name nameAttribute = AttributeUtil.getNameFromAttributes(attributes);
        String nameValue = nameAttribute == null ? null : nameAttribute.getNameValue();
        String requestedUsername = stringAttribute(attributes, "username");
        final String username = requestedUsername == null ? nameValue : requestedUsername;
        if (username == null || username.isBlank()) {
            throw new ConnectorException("Rocket.Chat user creation requires a username");
        }
        if (nameValue != null && !nameValue.equals(username)) {
            throw new ConnectorException("Rocket.Chat NAME and username attributes must match");
        }

        String displayName = stringAttribute(attributes, "name");
        String email = stringAttribute(attributes, "email");
        GuardedString password = AttributeUtil.getPasswordValue(attributes);
        if (displayName == null || displayName.isBlank() || email == null || email.isBlank() || password == null) {
            throw new ConnectorException("Rocket.Chat user creation requires a display name, email, and protected password");
        }

        ArrayList<String> roles = new ArrayList<>();
        Attribute rolesAttribute = AttributeUtil.find("roles", attributes);
        if (rolesAttribute != null) {
            for (Object value : rolesAttribute.getValue()) {
                if (!(value instanceof String role)) {
                    throw new ConnectorException("Rocket.Chat roles must be strings");
                }
                if (!roles.contains(role)) roles.add(role);
            }
        }
        if (roles.isEmpty()) roles.add("user");

        RocketChatConfiguration configuration = (RocketChatConfiguration) getConfiguration();
        String baseAddress = configuration.getBaseAddress().replaceAll("/+$", "");
        AtomicReference<byte[]> requestBody = new AtomicReference<>();
        AtomicReference<RuntimeException> serializationFailure = new AtomicReference<>();
        password.access(chars -> {
            try {
                Map<String, Object> payload = new HashMap<>();
                payload.put("username", username);
                payload.put("name", displayName);
                payload.put("email", email);
                payload.put("password", new String(chars));
                payload.put("roles", roles);
                requestBody.set(JSON.writeValueAsBytes(payload));
                payload.remove("password");
            } catch (IOException e) {
                serializationFailure.set(new ConnectorException("Rocket.Chat create request could not be serialized", e));
            }
        });
        if (serializationFailure.get() != null) throw serializationFailure.get();
        byte[] body = requestBody.get();
        if (body == null) throw new ConnectorException("Rocket.Chat create request body was not produced");

        try {
            HttpRequest request = HttpRequest.newBuilder(URI.create(baseAddress + "/users.create"))
                    .timeout(Duration.ofSeconds(configuration.getTimeoutSeconds()))
                    .version(HttpClient.Version.HTTP_1_1)
                    .header("X-Auth-Token", secretValue(configuration.getRestTokenValue()))
                    .header("X-User-Id", configuration.getRocketChatUserId())
                    .header("Accept", "application/json")
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofByteArray(body))
                    .build();
            HttpResponse<String> response = HTTP.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            if (response.statusCode() < 200 || response.statusCode() >= 300) {
                throw new ConnectorException("Rocket.Chat user creation returned HTTP status " + response.statusCode());
            }
            JsonNode root = JSON.readTree(response.body());
            if (!root.path("success").asBoolean(false)) {
                throw new ConnectorException("Rocket.Chat users.create reported an unsuccessful response");
            }
            JsonNode user = root.get("user");
            JsonNode returnedUid = user == null ? null : user.get("_id");
            if (returnedUid == null || !returnedUid.isTextual() || returnedUid.asText().isBlank()) {
                throw new ConnectorException("Rocket.Chat users.create response has no immutable user ID");
            }
            return new Uid(returnedUid.asText());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new ConnectorException("Rocket.Chat user creation was interrupted", e);
        } catch (IOException e) {
            throw new ConnectorException("Rocket.Chat user creation request failed", e);
        } finally {
            java.util.Arrays.fill(body, (byte) 0);
        }
    }

    /**
     * Update only the identity fields agreed for central management. Usernames,
     * roles, account state, passwords, and immutable IDs are deliberately not
     * writable through this connector.
     */
    @Override
    public Set<AttributeDelta> updateDelta(ObjectClass objectClass, Uid uid,
            Set<AttributeDelta> attributes, OperationOptions options) {
        if (!new ObjectClass("User").equals(objectClass)) {
            throw new UnsupportedOperationException("Rocket.Chat only supports updating user identity fields");
        }
        if (uid == null || uid.getUidValue() == null || uid.getUidValue().isBlank()) {
            throw new ConnectorException("Rocket.Chat user update requires an immutable UID");
        }

        Map<String, Object> data = new LinkedHashMap<>();
        for (AttributeDelta attribute : attributes) {
            String name = attribute.getName();
            if (!"name".equals(name) && !"email".equals(name)) {
                throw new ConnectorException("Rocket.Chat user update does not support attribute " + name);
            }
            if ((attribute.getValuesToAdd() != null && !attribute.getValuesToAdd().isEmpty())
                    || (attribute.getValuesToRemove() != null && !attribute.getValuesToRemove().isEmpty())
                    || attribute.getValuesToReplace() == null || attribute.getValuesToReplace().size() != 1) {
                throw new ConnectorException("Rocket.Chat user update requires one replacement value for " + name);
            }
            Object rawValue = attribute.getValuesToReplace().getFirst();
            if (!(rawValue instanceof String value) || value.isBlank()) {
                throw new ConnectorException("Rocket.Chat user update requires a non-empty " + name);
            }
            data.put(name, value);
        }
        if (data.isEmpty()) return Set.of();

        RocketChatConfiguration configuration = (RocketChatConfiguration) getConfiguration();
        String baseAddress = configuration.getBaseAddress().replaceAll("/+$", "");
        byte[] body;
        try {
            body = JSON.writeValueAsBytes(Map.of("userId", uid.getUidValue(), "data", data));
        } catch (IOException e) {
            throw new ConnectorException("Rocket.Chat update request could not be serialized", e);
        }

        try {
            HttpRequest request = HttpRequest.newBuilder(URI.create(baseAddress + "/users.update"))
                    .timeout(Duration.ofSeconds(configuration.getTimeoutSeconds()))
                    .version(HttpClient.Version.HTTP_1_1)
                    .header("X-Auth-Token", secretValue(configuration.getRestTokenValue()))
                    .header("X-User-Id", configuration.getRocketChatUserId())
                    .header("Accept", "application/json")
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofByteArray(body))
                    .build();
            HttpResponse<String> response = HTTP.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            if (response.statusCode() < 200 || response.statusCode() >= 300) {
                throw new ConnectorException("Rocket.Chat user update returned HTTP status " + response.statusCode());
            }
            JsonNode root = JSON.readTree(response.body());
            if (!root.path("success").asBoolean(false)) {
                throw new ConnectorException("Rocket.Chat users.update reported an unsuccessful response");
            }
            JsonNode user = root.get("user");
            JsonNode returnedUid = user == null ? null : user.get("_id");
            if (returnedUid == null || !returnedUid.isTextual() || !uid.getUidValue().equals(returnedUid.asText())) {
                throw new ConnectorException("Rocket.Chat users.update response did not match the requested immutable user ID");
            }
            return Set.of();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new ConnectorException("Rocket.Chat user update was interrupted", e);
        } catch (IOException e) {
            throw new ConnectorException("Rocket.Chat user update request failed", e);
        } finally {
            java.util.Arrays.fill(body, (byte) 0);
        }
    }

    private static String stringAttribute(Set<Attribute> attributes, String name) {
        Attribute attribute = AttributeUtil.find(name, attributes);
        return attribute == null ? null : AttributeUtil.getStringValue(attribute);
    }

    private static String secretValue(GuardedString secret) {
        AtomicReference<String> value = new AtomicReference<>();
        secret.access(chars -> value.set(new String(chars)));
        return value.get();
    }
}
