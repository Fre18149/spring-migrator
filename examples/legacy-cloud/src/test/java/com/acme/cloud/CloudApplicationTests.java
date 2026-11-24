package com.acme.cloud;

import static org.junit.Assert.assertEquals;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.Test;
import org.junit.runner.RunWith;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.junit4.SpringRunner;
import org.springframework.test.web.servlet.MockMvc;

@RunWith(SpringRunner.class)
@SpringBootTest
@AutoConfigureMockMvc
public class CloudApplicationTests {
    @Autowired MockMvc mvc;
    @Autowired InventoryClient inventory;

    @Test
    public void protectedEndpointNeedsToken() throws Exception {
        mvc.perform(get("/api/stock/abc")).andExpect(status().isUnauthorized());
    }

    @Test
    public void publicEndpointIsOpen() throws Exception {
        mvc.perform(get("/api/public/ping")).andExpect(status().isOk()).andExpect(content().string("pong"));
    }

    @Test
    public void trailingSlashStillMatches() throws Exception {
        mvc.perform(get("/api/public/ping/")).andExpect(status().isOk()).andExpect(content().string("pong"));
    }

    @Test
    public void protectedEndpointWithTrailingSlashStillNeedsToken() throws Exception {
        mvc.perform(get("/api/stock/abc/")).andExpect(status().isUnauthorized());
    }

    @Test
    public void circuitBreakerFallsBack() throws Exception {
        assertEquals(-1, inventory.stock("abc"));
    }
}
