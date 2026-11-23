package com.acme.legacy;

import java.util.List;
import javax.persistence.EntityManager;
import javax.persistence.PersistenceContext;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class AccountService {
    @PersistenceContext
    private EntityManager em;

    @Transactional
    public Account open(String owner, long cents) {
        Account a = new Account(owner, cents);
        em.persist(a);
        return a;
    }

    @Transactional(readOnly = true)
    public List<Account> byOwner(String owner) {
        return em.createNamedQuery("Account.byOwner", Account.class).setParameter("owner", owner).getResultList();
    }
}
