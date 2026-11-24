package com.acme.cloud;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public class PriceCalculatorTest {
    @Test
    public void addsTax() {
        assertEquals(119, new PriceCalculator().gross(100, "DE"));
    }
}
