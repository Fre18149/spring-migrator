FROM maven:3.8.6-eclipse-temurin-11 AS build
COPY . .
RUN mvn -q package
FROM eclipse-temurin:11-jre
FROM postgres:11
