import tools.jackson.databind.JsonNode
import tools.jackson.databind.node.JsonNodeFactory

objectClass("User") {
    search {
        endpoint("/users") {
            request {
                header("OCS-APIRequest", "true")
                queryParameter("format", "json")
            }
            objectExtractor {
                def body = response.body()
                def ocs = body instanceof JsonNode ? body.get("ocs") : null
                def meta = ocs instanceof JsonNode ? ocs.get("meta") : null
                def data = ocs instanceof JsonNode ? ocs.get("data") : null
                if (!(meta instanceof JsonNode) || meta.get("statuscode")?.asText() != "100") {
                    def code = meta instanceof JsonNode ? meta.get("statuscode")?.asText("missing") : "missing"
                    throw new IllegalStateException("ownCloud OCS users.list failed (status code: ${code})")
                }
                def users = data instanceof JsonNode ? data.get("users") : null
                if (!(users instanceof JsonNode) || !users.isArray()) throw new IllegalStateException("ownCloud OCS users.list response has no users collection")
                def projected = []
                users.elements().each { user ->
                    def uid = user.asText()
                    projected.add(JsonNodeFactory.instance.objectNode().put("_id", uid).put("uid", uid))
                }
                return projected
            }
            emptyFilterSupported true
        }
        endpoint("/users/{uid}") {
            request {
                header("OCS-APIRequest", "true")
                queryParameter("format", "json")
            }
            singleResult()
            objectExtractor {
                def body = response.body()
                def ocs = body instanceof JsonNode ? body.get("ocs") : null
                def meta = ocs instanceof JsonNode ? ocs.get("meta") : null
                def user = ocs instanceof JsonNode ? ocs.get("data") : null
                if (!(meta instanceof JsonNode) || meta.get("statuscode")?.asText() != "100") {
                    def code = meta instanceof JsonNode ? meta.get("statuscode")?.asText("missing") : "missing"
                    throw new IllegalStateException("ownCloud OCS users.info failed (status code: ${code})")
                }
                if (!(user instanceof JsonNode) || !user.isObject()) throw new IllegalStateException("ownCloud OCS users.info response has no user object")
                def projected = user.deepCopy().retain("id", "displayname", "email", "enabled")
                def enabled = projected.get("enabled")
                if (enabled != null && !enabled.isNull() && !enabled.isBoolean()) {
                    if (!enabled.isTextual() || !("true".equalsIgnoreCase(enabled.asText()) || "false".equalsIgnoreCase(enabled.asText()))) {
                        throw new IllegalStateException("ownCloud OCS users.info response has an invalid enabled value")
                    }
                    projected.put("enabled", Boolean.parseBoolean(enabled.asText()))
                }
                def id = projected.remove("id")
                if (id != null && !id.isNull()) {
                    projected.put("_id", id.asText())
                    projected.put("uid", id.asText())
                }
                return projected
            }
            supportedFilter(attribute("_id").eq().anySingleValue()) {
                request.pathParameter("uid", value)
            }
            supportedFilter(attribute("uid").eq().anySingleValue()) {
                request.pathParameter("uid", value)
            }
        }
    }
}
