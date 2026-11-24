package com.acme.cloud;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.cloud.client.circuitbreaker.EnableCircuitBreaker;

@SpringBootApplication
@EnableCircuitBreaker
public class CloudApplication {
    public static void main(String[] args) { SpringApplication.run(CloudApplication.class, args); }
}
