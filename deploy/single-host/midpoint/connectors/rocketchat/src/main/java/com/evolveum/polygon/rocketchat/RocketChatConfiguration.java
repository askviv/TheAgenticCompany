package com.evolveum.polygon.rocketchat;

import com.evolveum.polygon.scimrest.config.RestClientConfiguration;
import com.evolveum.polygon.scimrest.groovy.connector.BaseRestGroovyConnectorConfiguration;
import org.identityconnectors.framework.spi.ConfigurationProperty;
import org.identityconnectors.common.security.GuardedString;

/** Rocket.Chat's REST auth requires its token and immutable account ID headers. */
public class RocketChatConfiguration extends BaseRestGroovyConnectorConfiguration implements RestClientConfiguration.BearerTokenAuthorization {
    private GuardedString tokenValue;
    private String userId;

    @Override
    @ConfigurationProperty(groupMessageKey = "rest.auth.bearer", order = 505, required = true)
    public GuardedString getRestTokenValue() {
        return tokenValue;
    }

    public void setRestTokenValue(GuardedString tokenValue) {
        this.tokenValue = tokenValue;
    }

    @ConfigurationProperty(groupMessageKey = "rocketchat.auth", order = 506, required = true)
    public String getRocketChatUserId() {
        return userId;
    }

    public void setRocketChatUserId(String userId) {
        this.userId = userId;
    }
}
