package com.acme.shop.web;

import com.acme.shop.core.Product;
import com.acme.shop.core.ProductRepository;
import java.util.List;
import javax.validation.Valid;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.autoconfigure.domain.EntityScan;
import org.springframework.data.jpa.repository.config.EnableJpaRepositories;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@SpringBootApplication
@EntityScan("com.acme.shop.core")
@EnableJpaRepositories("com.acme.shop.core")
@RestController
public class ShopWebApplication {
    private final ProductRepository products;
    public ShopWebApplication(ProductRepository products) { this.products = products; }
    public static void main(String[] args) { SpringApplication.run(ShopWebApplication.class, args); }

    @PostMapping("/products")
    public Product create(@Valid @RequestBody Product p) { return products.save(p); }

    @GetMapping("/products")
    public List<Product> search(@RequestParam String q) { return products.search("%" + q + "%"); }
}
