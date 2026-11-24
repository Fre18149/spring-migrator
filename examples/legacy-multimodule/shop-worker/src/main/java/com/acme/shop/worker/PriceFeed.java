package com.acme.shop.worker;

import com.netflix.hystrix.contrib.javanica.annotation.HystrixCommand;
import org.springframework.stereotype.Service;

@Service
public class PriceFeed {
    @HystrixCommand(fallbackMethod = "cachedPrice")
    public long price(String sku) {
        throw new IllegalStateException("feed unavailable");
    }

    public long cachedPrice(String sku) {
        return 999;
    }
}
