package com.acme.legacy;

import java.util.List;
import javax.persistence.EntityManager;
import javax.persistence.PersistenceContext;
import org.apache.openjpa.persistence.OpenJPAEntityManager;
import org.apache.openjpa.persistence.OpenJPAPersistence;
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

    @Transactional(readOnly = true)
    public List<Account> richerThan(String owner, long cents) {
        return em.createQuery("select a from Account a where a.ownerName = ?1 and a.balanceCents > ?2", Account.class)
                .setParameter(1, owner)
                .setParameter(2, cents)
                .getResultList();
    }

    @Transactional
    public Object idOf(Account a) {
        OpenJPAEntityManager oem = OpenJPAPersistence.cast(em);
        return oem.getObjectId(a);
    }
}
