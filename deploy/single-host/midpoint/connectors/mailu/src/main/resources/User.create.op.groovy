objectClass("User") {
    create {
        endpoint(POST, "/user") {
            supportedAttributes "username", "displayed_name", "enabled", "enable_imap", "enable_pop", "allow_spoofing", "quota_bytes"
            request { contentType APPLICATION_JSON }
        }
    }
}
