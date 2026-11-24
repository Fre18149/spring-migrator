package com.acme.audit;

import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
@ConditionalOnWebApplication
public class AuditAutoConfiguration {
    @Bean
    public AuditFilter acmeAuditFilter() { return new AuditFilter(); }
}
