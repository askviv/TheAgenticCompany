objectClass("User") {
    create {
        endpoint(POST, "/users.create") {
            supportedAttributes "username", "name", "email", "password", "roles"
            request { contentType APPLICATION_JSON }
        }
    }
}
