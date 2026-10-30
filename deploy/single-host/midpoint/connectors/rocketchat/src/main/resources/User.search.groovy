objectClass("User") {
    search {
        endpoint("/users.list") {
            objectExtractor {
                def root = response.body()
                def users = root?.get("users")
                if (users == null || !users.isArray()) throw new IllegalStateException("Rocket.Chat users.list response has no users array")
                def projected = []
                users.elements().each { user -> projected.add(projectUser(user)) }
                return projected
            }
            pagingSupport {
                request.queryParameter("count", paging.pageSize)
                       .queryParameter("offset", Math.max(0, paging.pageOffset - 1))
            }
            supportedFilter(attribute("email").eq().anySingleValue()) {
                request.queryParameter("query", groovy.json.JsonOutput.toJson(["emails.address": value]))
            }
            emptyFilterSupported true
        }
        endpoint("/users.info") {
            singleResult()
            objectExtractor {
                def root = response.body()
                def user = root?.get("user")
                if (user == null || !user.isObject()) throw new IllegalStateException("Rocket.Chat users.info response has no user object")
                return projectUser(user)
            }
            supportedFilter(attribute("_id").eq().anySingleValue()) {
                request.queryParameter("userId", value)
            }
        }
    }
}

def projectUser(user) {
    def projected = user.deepCopy().retain("_id", "username", "name", "active", "type", "roles")
    def emails = user.get("emails")
    if (emails != null && emails.isArray() && !emails.isEmpty()) {
        def address = emails.get(0)?.get("address")
        if (address != null && !address.isNull()) projected.put("email", address.asText())
    }
    return projected
}
