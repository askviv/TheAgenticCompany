objectClass("User") {
    update {
        endpoint(POST, "/users.update") {
            supportedAttributes "name", "email"
            request { contentType APPLICATION_JSON }
        }
    }
}
