package com.acme.cloud;

import org.springframework.stereotype.Component;

@Component
public class PriceCalculator {
    public long gross(long netCents, String country) { return netCents + netCents * TaxRates.percent(country) / 100; }
}
