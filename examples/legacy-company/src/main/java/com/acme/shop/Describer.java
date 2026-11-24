package com.acme.shop;

import org.jvnet.jaxb2_commons.lang.JAXBToStringStrategy;

/** Uses the jaxb2-basics runtime that the company parent used to manage. */
public final class Describer {
    private Describer() {}

    public static String strategy() {
        return JAXBToStringStrategy.class.getSimpleName();
    }
}
