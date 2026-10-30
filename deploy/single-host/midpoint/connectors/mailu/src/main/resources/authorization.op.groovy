authentication {
    rest {
        bearer {
            implementation {
                request.header("Authorization", "Bearer " + decrypt(configuration.restTokenValue))
            }
        }
    }
}
