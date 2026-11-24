package com.acme.persistence;

import javax.persistence.EntityManager;
import org.apache.openjpa.persistence.OpenJPAPersistence;

/** Company persistence helper (openjpa). */
public final class AcmeIds {
    private AcmeIds() {}

    public static Object idOf(EntityManager em, Object entity) {
        
        return OpenJPAPersistence.cast(em).getObjectId(entity);
    }
}
