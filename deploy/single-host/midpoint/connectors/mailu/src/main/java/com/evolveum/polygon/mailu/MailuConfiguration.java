package com.evolveum.polygon.mailu;

import com.evolveum.polygon.scimrest.config.RestClientConfiguration;
import com.evolveum.polygon.scimrest.groovy.connector.BaseRestGroovyConnectorConfiguration;
import org.identityconnectors.common.security.GuardedString;
import org.identityconnectors.framework.spi.ConfigurationProperty;

/** Mailu administration API uses a bearer token for its API_TOKEN. */
public class MailuConfiguration extends BaseRestGroovyConnectorConfiguration implements RestClientConfiguration.BearerTokenAuthorization {
    private GuardedString tokenValue;

    @Override
    @ConfigurationProperty(groupMessageKey = "rest.auth.bearer", order = 505, required = true)
    public GuardedString getRestTokenValue() {
        return tokenValue;
    }

    public void setRestTokenValue(GuardedString tokenValue) {
        this.tokenValue = tokenValue;
    }
}
