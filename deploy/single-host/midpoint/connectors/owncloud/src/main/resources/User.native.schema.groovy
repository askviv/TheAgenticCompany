objectClass("User") {
    attribute("_id") { jsonType "string"; creatable false; updatable false; description "Immutable ownCloud provider identifier" }
    attribute("uid") { jsonType "string"; creatable true; updatable false; description "Immutable ownCloud account name" }
    attribute("displayname") { jsonType "string"; updatable true; description "Centrally managed ownCloud display name" }
    attribute("email") { jsonType "string"; updatable true; description "Centrally managed ownCloud email address" }
    attribute("enabled") { jsonType "boolean"; description "Whether the ownCloud account is enabled" }
    attribute("password") { jsonType "string"; creatable true; readable false; updatable false; description "Initial password required when creating an ownCloud account" }
}
