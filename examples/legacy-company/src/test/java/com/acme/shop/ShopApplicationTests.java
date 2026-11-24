package com.acme.shop;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;

@SpringBootTest
@AutoConfigureMockMvc
class ShopApplicationTests {
    @Autowired MockMvc mvc;

    @Test
    void jaxbRuntimeIsAvailable() {
        org.junit.jupiter.api.Assertions.assertEquals("JAXBToStringStrategy", Describer.strategy());
    }

    /** The company audit starter must still be auto-configured after the migration. */
    @Test
    void companyAuditFilterIsActive() throws Exception {
        mvc.perform(get("/hello")).andExpect(status().isOk()).andExpect(header().string("X-Audit", "acme"));
    }
}
