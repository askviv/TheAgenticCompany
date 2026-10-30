package com.evolveum.polygon.owncloud;

import com.evolveum.polygon.scimrest.config.RestClientConfiguration;
import com.evolveum.polygon.scimrest.groovy.connector.BaseRestGroovyConnectorConfiguration;
import org.identityconnectors.common.security.GuardedString;
import org.identityconnectors.framework.spi.ConfigurationProperty;

/** Basic authentication configuration for the ownCloud OCS Provisioning API. */
public class OwnCloudConfiguration extends BaseRestGroovyConnectorConfiguration
        implements RestClientConfiguration.BasicAuthorization {
    private String username;
    private GuardedString password;

    @Override
    @ConfigurationProperty(groupMessageKey = "rest.auth.basic", order = 500, required = true)
    public String getRestUsername() {
        return username;
    }

    public void setRestUsername(String username) {
        this.username = username;
    }

    @Override
    @ConfigurationProperty(groupMessageKey = "rest.auth.basic", order = 505, required = true)
    public GuardedString getRestPassword() {
        return password;
    }

    public void setRestPassword(GuardedString password) {
        this.password = password;
    }
}
