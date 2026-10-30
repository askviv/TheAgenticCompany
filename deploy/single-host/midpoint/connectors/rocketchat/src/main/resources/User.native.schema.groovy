objectClass("User") {
    attribute("_id") { jsonType "string"; creatable false; updatable false; description "Immutable Rocket.Chat user identifier" }
    attribute("username") { jsonType "string"; creatable true; updatable false; description "Rocket.Chat username" }
    attribute("name") { jsonType "string"; creatable true; updatable true; description "Centrally managed display name" }
    attribute("email") { jsonType "string"; creatable true; updatable true; description "Centrally managed primary email address" }
    attribute("active") { jsonType "boolean"; creatable false; updatable false; description "Whether the Rocket.Chat account is active" }
    attribute("type") { jsonType "string"; creatable false; updatable false; description "Rocket.Chat account type" }
    attribute("roles") { jsonType "string"; creatable true; multiValued true; updatable false; description "Assigned Rocket.Chat roles" }
    attribute("password") { jsonType "string"; readable false; updatable false; description "Initial password for a newly created account" }
}
