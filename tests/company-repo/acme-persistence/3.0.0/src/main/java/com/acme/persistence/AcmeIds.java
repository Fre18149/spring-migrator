package com.acme.persistence;

import jakarta.persistence.EntityManager;
import org.hibernate.Session;

/** Company persistence helper (h7). */
public final class AcmeIds {
    private AcmeIds() {}

    public static Object idOf(EntityManager em, Object entity) {
        Session.class.getName(); // compiled against Hibernate
        return em.getEntityManagerFactory().getPersistenceUnitUtil().getIdentifier(entity);
    }
}
