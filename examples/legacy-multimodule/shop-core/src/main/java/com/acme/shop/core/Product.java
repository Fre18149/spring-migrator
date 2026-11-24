package com.acme.shop.core;

import javax.persistence.Entity;
import javax.persistence.GeneratedValue;
import javax.persistence.Id;
import javax.validation.constraints.NotBlank;

@Entity
public class Product {
    @Id
    @GeneratedValue
    private Long id;
    @NotBlank
    private String name;

    protected Product() {}
    public Product(String name) { this.name = name; }
    public Long getId() { return id; }
    public String getName() { return name; }
}
