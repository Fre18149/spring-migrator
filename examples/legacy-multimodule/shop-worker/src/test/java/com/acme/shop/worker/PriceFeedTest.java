package com.acme.shop.worker;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest
class PriceFeedTest {
    @Autowired PriceFeed feed;

    @Test
    void fallsBackWhenTheFeedFails() {
        assertEquals(999, feed.price("abc"));
    }
}
