package com.acme.shop.web;

import io.swagger.v3.oas.models.OpenAPI;
import io.swagger.v3.oas.models.info.Info;
import org.springdoc.core.GroupedOpenApi;
import org.springdoc.core.customizers.OpenApiCustomiser;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class SwaggerConfig {
    @Bean
    public GroupedOpenApi productsApi() {
        return GroupedOpenApi.builder().group("products").pathsToMatch("/products/**")
                .addOpenApiCustomiser(serverCustomiser()).build();
    }

    @Bean
    public OpenApiCustomiser serverCustomiser() {
        return openApi -> openApi.info(new Info().title("Shop").version("1"));
    }

    @Bean
    public OpenAPI shopOpenApi() {
        return new OpenAPI().info(new Info().title("Shop API"));
    }
}
