package com.acme.shop.web;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.Test;
import org.junit.runner.RunWith;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.junit4.SpringRunner;
import org.springframework.test.web.servlet.MockMvc;

@RunWith(SpringRunner.class)
@SpringBootTest
@AutoConfigureMockMvc
public class ShopWebApplicationTests {
    @Autowired MockMvc mvc;

    @Test
    public void createAndSearchAcrossModules() throws Exception {
        mvc.perform(post("/products").contentType(MediaType.APPLICATION_JSON).content("{\"name\":\"blue chair\"}")).andExpect(status().isOk());
        mvc.perform(get("/products").param("q", "chair")).andExpect(status().isOk()).andExpect(jsonPath("$[0].name").value("blue chair"));
    }

    @Test
    public void trailingSlashStillWorks() throws Exception {
        mvc.perform(get("/products/").param("q", "x")).andExpect(status().isOk());
    }

    @Test
    public void validationStillApplies() throws Exception {
        mvc.perform(post("/products").contentType(MediaType.APPLICATION_JSON).content("{\"name\":\"\"}")).andExpect(status().isBadRequest());
    }
}
