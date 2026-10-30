authentication {
    rest {
        bearer {
            implementation {
                // Rocket.Chat 5.3's HTTP listener does not complete JDK HttpClient's h2c upgrade.
                request.version(com.evolveum.polygon.scimrest.groovy.api.HttpVersion.HTTP_1_1)
                request.header("X-Auth-Token", decrypt(configuration.restTokenValue))
                request.header("X-User-Id", configuration.rocketChatUserId)
            }
        }
    }
}
