package com.evolveum.polygon.mailu;

import com.evolveum.polygon.scimrest.groovy.impl.ManifestBasedConnector;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicReference;
import org.identityconnectors.common.security.GuardedString;
import org.identityconnectors.framework.common.exceptions.AlreadyExistsException;
import org.identityconnectors.framework.common.exceptions.ConnectorException;
import org.identityconnectors.framework.common.objects.Attribute;
import org.identityconnectors.framework.common.objects.AttributeDelta;
import org.identityconnectors.framework.common.objects.AttributeUtil;
import org.identityconnectors.framework.common.objects.Name;
import org.identityconnectors.framework.common.objects.ObjectClass;
import org.identityconnectors.framework.common.objects.OperationOptions;
import org.identityconnectors.framework.common.objects.Uid;
import org.identityconnectors.framework.spi.ConnectorClass;

@ConnectorClass(displayNameKey = "mailu.rest.display", configurationClass = MailuConfiguration.class, messageCatalogPaths = "Messages")
public class MailuConnector extends ManifestBasedConnector {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final HttpClient HTTP = HttpClient.newBuilder()
            .version(HttpClient.Version.HTTP_1_1)
            .followRedirects(HttpClient.Redirect.NEVER)
            .build();

    /**
     * Mailu uses the mailbox email as its immutable UID, while ConnId requires
     * a distinct NAME input on create. The connector maps that NAME to the API
     * email field and returns the same value as UID after a successful create.
     */
    @Override
    public Uid create(ObjectClass objectClass, Set<Attribute> attributes, OperationOptions options) {
        if (!new ObjectClass("User").equals(objectClass)) {
            throw new UnsupportedOperationException("Mailu only supports creating mailbox accounts");
        }
        if (AttributeUtil.getUidAttribute(attributes) != null) {
            throw new ConnectorException("Mailu mailbox creation must not include an immutable UID");
        }

        Name name = AttributeUtil.getNameFromAttributes(attributes);
        String email = name == null ? null : name.getNameValue();
        if (email == null || email.isBlank()) {
            throw new ConnectorException("Mailu mailbox creation requires an email address as NAME");
        }
        String displayName = stringAttribute(attributes, "displayed_name");

        GuardedString password = AttributeUtil.getPasswordValue(attributes);
        if (password == null) {
            throw new ConnectorException("Mailu mailbox creation requires a protected password");
        }

        MailuConfiguration configuration = (MailuConfiguration) getConfiguration();
        String baseAddress = configuration.getBaseAddress().replaceAll("/+$", "");
        AtomicReference<byte[]> requestBody = new AtomicReference<>();
        AtomicReference<RuntimeException> serializationFailure = new AtomicReference<>();
        final String mailboxDisplayName = displayName;
        password.access(chars -> {
            try {
                Map<String, Object> payload = new LinkedHashMap<>();
                payload.put("email", email);
                if (mailboxDisplayName != null && !mailboxDisplayName.isBlank()) {
                    payload.put("displayed_name", mailboxDisplayName);
                }
                payload.put("raw_password", new String(chars));
                payload.put("enabled", booleanAttribute(attributes, "enabled", true));
                payload.put("enable_imap", booleanAttribute(attributes, "enable_imap", true));
                payload.put("enable_pop", booleanAttribute(attributes, "enable_pop", false));
                payload.put("allow_spoofing", booleanAttribute(attributes, "allow_spoofing", false));
                Attribute quota = AttributeUtil.find("quota_bytes", attributes);
                if (quota != null) payload.put("quota_bytes", AttributeUtil.getSingleValue(quota));
                requestBody.set(JSON.writeValueAsBytes(payload));
                payload.remove("raw_password");
            } catch (IOException e) {
                serializationFailure.set(new ConnectorException("Mailu create request could not be serialized", e));
            }
        });
        if (serializationFailure.get() != null) throw serializationFailure.get();
        byte[] body = requestBody.get();
        if (body == null) throw new ConnectorException("Mailu create request body was not produced");

        try {
            HttpRequest request = HttpRequest.newBuilder(URI.create(baseAddress + "/user"))
                    .timeout(Duration.ofSeconds(configuration.getTimeoutSeconds()))
                    .version(HttpClient.Version.HTTP_1_1)
                    .header("Authorization", "Bearer " + secretValue(configuration.getRestTokenValue()))
                    .header("Accept", "application/json")
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofByteArray(body))
                    .build();
            HttpResponse<String> response = HTTP.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            if (response.statusCode() == 409) {
                throw new AlreadyExistsException("Mailu mailbox already exists").initUid(new Uid(email));
            }
            if (response.statusCode() < 200 || response.statusCode() >= 300) {
                throw new ConnectorException("Mailu mailbox creation returned HTTP status " + response.statusCode());
            }
            if (!response.body().isBlank()) {
                JsonNode root = JSON.readTree(response.body());
                JsonNode returnedEmail = root.get("email");
                if (returnedEmail == null && root.has("user")) returnedEmail = root.path("user").get("email");
                if (returnedEmail != null && (!returnedEmail.isTextual() || !email.equalsIgnoreCase(returnedEmail.asText()))) {
                    throw new ConnectorException("Mailu create response did not match the requested mailbox");
                }
            }
            return new Uid(email);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new ConnectorException("Mailu mailbox creation was interrupted", e);
        } catch (IOException e) {
            throw new ConnectorException("Mailu mailbox creation request failed", e);
        } finally {
            java.util.Arrays.fill(body, (byte) 0);
        }
    }

    /** Update only the mailbox display name; the mailbox address is its immutable UID. */
    @Override
    public Set<AttributeDelta> updateDelta(ObjectClass objectClass, Uid uid,
            Set<AttributeDelta> attributes, OperationOptions options) {
        if (!new ObjectClass("User").equals(objectClass)) {
            throw new UnsupportedOperationException("Mailu only supports updating mailbox identity fields");
        }
        if (uid == null || uid.getUidValue() == null || uid.getUidValue().isBlank()) {
            throw new ConnectorException("Mailu mailbox update requires its immutable email UID");
        }
        if (attributes.isEmpty()) return Set.of();

        Map<String, Object> payload = new LinkedHashMap<>();
        for (AttributeDelta attribute : attributes) {
            if (!"displayed_name".equals(attribute.getName())) {
                throw new ConnectorException("Mailu mailbox update does not support attribute " + attribute.getName());
            }
            if ((attribute.getValuesToAdd() != null && !attribute.getValuesToAdd().isEmpty())
                    || (attribute.getValuesToRemove() != null && !attribute.getValuesToRemove().isEmpty())
                    || attribute.getValuesToReplace() == null || attribute.getValuesToReplace().size() != 1) {
                throw new ConnectorException("Mailu mailbox update requires one replacement value for displayed_name");
            }
            Object rawValue = attribute.getValuesToReplace().getFirst();
            if (!(rawValue instanceof String value) || value.isBlank()) {
                throw new ConnectorException("Mailu mailbox update requires a non-empty displayed_name");
            }
            payload.put("displayed_name", value);
        }

        MailuConfiguration configuration = (MailuConfiguration) getConfiguration();
        String baseAddress = configuration.getBaseAddress().replaceAll("/+$", "");
        String encodedEmail = java.net.URLEncoder.encode(uid.getUidValue(), StandardCharsets.UTF_8).replace("+", "%20");
        byte[] body;
        try {
            body = JSON.writeValueAsBytes(payload);
        } catch (IOException e) {
            throw new ConnectorException("Mailu update request could not be serialized", e);
        }

        try {
            HttpRequest request = HttpRequest.newBuilder(URI.create(baseAddress + "/user/" + encodedEmail))
                    .timeout(Duration.ofSeconds(configuration.getTimeoutSeconds()))
                    .version(HttpClient.Version.HTTP_1_1)
                    .header("Authorization", "Bearer " + secretValue(configuration.getRestTokenValue()))
                    .header("Accept", "application/json")
                    .header("Content-Type", "application/json")
                    .method("PATCH", HttpRequest.BodyPublishers.ofByteArray(body))
                    .build();
            HttpResponse<String> response = HTTP.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            if (response.statusCode() < 200 || response.statusCode() >= 300) {
                throw new ConnectorException("Mailu mailbox update returned HTTP status " + response.statusCode());
            }
            if (!response.body().isBlank()) {
                JsonNode root = JSON.readTree(response.body());
                JsonNode returnedEmail = root.get("email");
                if (returnedEmail != null && (!returnedEmail.isTextual()
                        || !uid.getUidValue().equalsIgnoreCase(returnedEmail.asText()))) {
                    throw new ConnectorException("Mailu mailbox update response did not match the requested immutable email UID");
                }
            }
            return Set.of();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new ConnectorException("Mailu mailbox update was interrupted", e);
        } catch (IOException e) {
            throw new ConnectorException("Mailu mailbox update request failed", e);
        } finally {
            java.util.Arrays.fill(body, (byte) 0);
        }
    }

    private static String stringAttribute(Set<Attribute> attributes, String name) {
        Attribute attribute = AttributeUtil.find(name, attributes);
        return attribute == null ? null : AttributeUtil.getStringValue(attribute);
    }

    private static boolean booleanAttribute(Set<Attribute> attributes, String name, boolean defaultValue) {
        Attribute attribute = AttributeUtil.find(name, attributes);
        if (attribute == null) return defaultValue;
        Boolean value = AttributeUtil.getBooleanValue(attribute);
        if (value == null) throw new ConnectorException("Mailu create attribute " + name + " must be a Boolean");
        return value;
    }

    private static String secretValue(GuardedString secret) {
        AtomicReference<String> value = new AtomicReference<>();
        secret.access(chars -> value.set(new String(chars)));
        return value.get();
    }
}
