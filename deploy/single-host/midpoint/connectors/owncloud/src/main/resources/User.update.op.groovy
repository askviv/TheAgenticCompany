objectClass("User") {
    update {
        endpoint(PUT, "/users/{uid}") {
            supportedAttributes "displayname", "email"
        }
    }
}
