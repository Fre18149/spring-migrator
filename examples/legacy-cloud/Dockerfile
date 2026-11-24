FROM maven:3.8-openjdk-11 AS build
COPY . /src
RUN cd /src && mvn -q -DskipTests package
FROM openjdk:11-jre-slim
COPY --from=build /src/target/legacy-cloud-1.0.0.jar /app.jar
ENTRYPOINT ["java", "-jar", "/app.jar"]
