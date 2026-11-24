FROM openjdk:11-jre
COPY target/legacy-openjpa-1.0.0.jar /app/app.jar
COPY lib/openjpa-3.2.2.jar /app/openjpa-3.2.2.jar
ENTRYPOINT ["java", "-javaagent:/app/openjpa-3.2.2.jar", "-jar", "/app/app.jar"]
