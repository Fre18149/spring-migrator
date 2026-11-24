package com.acme.shop.worker;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.cloud.netflix.hystrix.EnableHystrix;

@SpringBootApplication
@EnableHystrix
public class WorkerApplication {
    public static void main(String[] args) { SpringApplication.run(WorkerApplication.class, args); }
}
