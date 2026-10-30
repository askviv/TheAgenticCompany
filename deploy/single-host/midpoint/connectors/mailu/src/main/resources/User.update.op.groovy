objectClass("User") {
    update {
        endpoint(PATCH, "/user/{email}") {
            supportedAttributes "displayed_name"
            request { contentType APPLICATION_JSON }
        }
    }
}
