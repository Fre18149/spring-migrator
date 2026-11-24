package com.acme.audit;

import org.springframework.boot.autoconfigure.AutoConfiguration;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.annotation.Bean;

@AutoConfiguration
@ConditionalOnWebApplication
public class AuditAutoConfiguration {
    @Bean
    public AuditFilter acmeAuditFilter() { return new AuditFilter(); }
}
