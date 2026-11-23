package com.acme.legacy;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.test.context.junit.jupiter.SpringJUnitConfig;

@SpringJUnitConfig(classes = {JpaConfig.class, AccountService.class})
class AccountServiceTest {
    @Autowired AccountService service;

    @Test
    void persistsAndQueries() {
        Account a = service.open("alice", 1500);
        assertNotNull(a.getId());
        assertEquals(1, service.byOwner("alice").size());
    }
}
