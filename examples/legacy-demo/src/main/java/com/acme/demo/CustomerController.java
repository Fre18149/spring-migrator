package com.acme.demo;

import java.util.List;
import javax.servlet.http.HttpServletRequest;
import javax.validation.Valid;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/customers")
public class CustomerController {
    private final CustomerRepository repo;
    public CustomerController(CustomerRepository repo) { this.repo = repo; }

    @GetMapping
    public List<Customer> all(HttpServletRequest req) { return repo.findAll(); }

    @PostMapping
    public Customer create(@Valid @RequestBody Customer c) { return repo.save(c); }
}
