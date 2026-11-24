package com.acme.legacy;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;

import java.net.URL;
import javax.persistence.EntityManagerFactory;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.context.junit.jupiter.SpringJUnitConfig;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.transaction.PlatformTransactionManager;

@SpringJUnitConfig(classes = {JpaConfig.class, AccountService.class})
class AccountServiceTest {
    @Autowired AccountService service;
    @Autowired EntityManagerFactory emf;
    @Autowired PlatformTransactionManager tx;

    @Test
    void persistsAndQueries() {
        Account a = service.open("alice", 1500);
        assertNotNull(a.getId());
        assertEquals(1, service.byOwner("alice").size());
    }

    @Test
    void positionalParameters() {
        service.open("bob", 100);
        service.open("bob", 5000);
        assertEquals(1, service.richerThan("bob", 1000).size());
    }

    @Test
    void identifierLookupAndConverter() throws Exception {
        Account a = service.open("carol", 1);
        assertEquals(a.getId(), service.idOf(a));
        Long id = a.getId();
        new TransactionTemplate(tx).executeWithoutResult(s -> {
            javax.persistence.EntityManager em = org.springframework.orm.jpa.EntityManagerFactoryUtils.getTransactionalEntityManager(emf);
            try { em.find(Account.class, id).setWebsite(new URL("https://acme.example/carol")); } catch (Exception e) { throw new RuntimeException(e); }
        });
        javax.persistence.EntityManager em = emf.createEntityManager();
        Account reloaded = em.find(Account.class, id);
        assertEquals("https://acme.example/carol", reloaded.getWebsite().toString());
        assertEquals("system", reloaded.getOpenedBy());
        em.close();
    }
}
