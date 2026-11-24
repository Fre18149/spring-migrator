package com.acme.cloud;

public final class TaxRates {
    private TaxRates() {}
    public static int percent(String country) { return "DE".equals(country) ? 19 : 20; }
}
