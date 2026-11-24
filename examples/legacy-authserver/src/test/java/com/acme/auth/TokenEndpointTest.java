package com.acme.auth;

import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.httpBasic;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.hamcrest.Matchers;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;

@SpringBootTest
@AutoConfigureMockMvc
class TokenEndpointTest {
    @Autowired MockMvc mvc;

    @Test
    void clientCredentialsTokenAtLegacyEndpoint() throws Exception {
        mvc.perform(post("/oauth/token").with(httpBasic("billing", "billing-secret"))
                .param("grant_type", "client_credentials").param("scope", "invoices.read"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.access_token").isString())
            .andExpect(jsonPath("$.expires_in", Matchers.lessThanOrEqualTo(600)))
            .andExpect(jsonPath("$.expires_in", Matchers.greaterThan(500)));
    }

    @Test
    void wrongSecretIsRejected() throws Exception {
        mvc.perform(post("/oauth/token").with(httpBasic("billing", "nope")).param("grant_type", "client_credentials"))
            .andExpect(status().isUnauthorized());
    }
}
