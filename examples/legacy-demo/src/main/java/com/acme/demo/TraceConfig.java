package com.acme.demo;

import org.springframework.boot.actuate.trace.http.HttpTraceRepository;
import org.springframework.boot.actuate.trace.http.InMemoryHttpTraceRepository;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class TraceConfig {
    @Bean
    public HttpTraceRepository httpTraceRepository() {
        InMemoryHttpTraceRepository repo = new InMemoryHttpTraceRepository();
        repo.setCapacity(50);
        return repo;
    }
}
