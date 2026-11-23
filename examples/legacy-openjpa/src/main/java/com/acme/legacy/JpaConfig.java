package com.acme.legacy;

import javax.persistence.EntityManagerFactory;
import javax.sql.DataSource;
import org.apache.openjpa.persistence.PersistenceProviderImpl;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.orm.jpa.JpaTransactionManager;
import org.springframework.orm.jpa.LocalContainerEntityManagerFactoryBean;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.annotation.EnableTransactionManagement;

@Configuration
@EnableTransactionManagement
public class JpaConfig {
    @Bean
    DataSource dataSource() {
        return new DriverManagerDataSource("jdbc:h2:mem:legacy;DB_CLOSE_DELAY=-1", "sa", "");
    }

    @Bean
    LocalContainerEntityManagerFactoryBean entityManagerFactory(DataSource ds) {
        LocalContainerEntityManagerFactoryBean f = new LocalContainerEntityManagerFactoryBean();
        f.setDataSource(ds);
        f.setPersistenceUnitName("legacy");
        f.setPersistenceProvider(new PersistenceProviderImpl());
        return f;
    }

    @Bean
    PlatformTransactionManager transactionManager(EntityManagerFactory emf) {
        return new JpaTransactionManager(emf);
    }
}
