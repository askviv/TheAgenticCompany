objectClass("User") {
    attribute("email") { jsonType "string"; creatable false; updatable false; description "Mailbox address and immutable account key" }
    attribute("username") { jsonType "string"; creatable true; updatable false; description "Mailbox address used as the ConnId account name on create" }
    attribute("displayed_name") { jsonType "string"; creatable true; updatable true; description "Centrally managed mailbox display name" }
    attribute("enabled") { jsonType "boolean"; creatable true; description "Whether the mailbox is enabled" }
    attribute("enable_imap") { jsonType "boolean"; creatable true; description "Whether IMAP access is enabled" }
    attribute("enable_pop") { jsonType "boolean"; creatable true; description "Whether POP access is enabled" }
    attribute("allow_spoofing") { jsonType "boolean"; creatable true; description "Whether sender spoofing is allowed" }
    attribute("quota_bytes") { jsonType "integer"; creatable true; description "Mailbox storage limit in bytes" }
    attribute("quota_bytes_used") { jsonType "integer"; description "Mailbox storage used in bytes" }
}
