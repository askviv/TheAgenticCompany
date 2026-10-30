objectClass("User") {
    search {
        endpoint("/user") {
            responseFormat JSON_ARRAY
            objectExtractor {
                def body = response.body()
                if (body == null || !body.isArray()) throw new IllegalStateException("Mailu user list response is not an array")
                def projected = []
                body.elements().each { user -> projected.add(projectUser(user)) }
                return projected
            }
            emptyFilterSupported true
        }
        endpoint("/user/{email}") {
            singleResult()
            objectExtractor {
                def body = response.body()
                def user = body?.get("user")
                if (user == null) user = body
                if (user == null || !user.isObject()) throw new IllegalStateException("Mailu user response is not an object")
                return projectUser(user)
            }
            supportedFilter(attribute("email").eq().anySingleValue()) {
                request.pathParameter("email", value)
            }
        }
    }
}

def projectUser(user) {
    def projected = user.deepCopy().retain("email", "displayed_name", "enabled", "enable_imap", "enable_pop", "allow_spoofing", "quota_bytes", "quota_bytes_used")
    def email = projected.get("email")
    if (email == null || !email.isTextual() || email.asText().isBlank()) {
        throw new IllegalStateException("Mailu user response has no mailbox address")
    }
    projected.put("username", email.asText())
    return projected
}
