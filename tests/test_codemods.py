"""Unit tests for the codemods/fixers that do not need a build (python3 -m unittest discover tests)."""
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spring_migrate as sm  # noqa: E402

sm.OFFLINE = True


class Project:
    def __init__(self, files: dict):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name, text in files.items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")

    def __getitem__(self, name):
        return (self.root / name).read_text(encoding="utf-8")

    def exists(self, name):
        return (self.root / name).exists()

    def close(self):
        self.tmp.cleanup()


POM = """
<project>
  <parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId><version>2.7.13</version></parent>
  <properties>
    <java.version>11</java.version>
    <mockito.version>4.11.0</mockito.version>
  </properties>
  <dependencies>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency>
    DEPS
  </dependencies>
</project>
"""


def pom(deps=""):
    return POM.replace("DEPS", deps)


class ConfigFileTest(unittest.TestCase):
    def test_yaml_flatten_remove_put(self):
        pr = Project({"src/main/resources/application.yml": """
            spring:
              application:
                name: demo
            zuul:
              routes:
                inv:
                  path: /inv/**
            list:
            - a
            - b
            ---
            spring.config.activate.on-profile: dev
            """})
        c = sm.ConfigFile(pr.root / "src/main/resources/application.yml")
        keys = {e["key"]: e["value"] for e in c.entries()}
        self.assertEqual(keys["zuul.routes.inv.path"], "/inv/**")
        self.assertEqual(keys["list[1]"], "b")
        c.remove("zuul.routes.inv.path")
        c.put("x.y", "/a/**")
        c.save()
        text = pr["src/main/resources/application.yml"]
        self.assertNotIn("zuul", text)
        self.assertIn("list:\n- a", text)
        self.assertIn("x.y: /a/**\n---", text)
        pr.close()


class NetflixTest(unittest.TestCase):
    def test_zuul_routes_to_gateway_mvc(self):
        pr = Project({
            "pom.xml": pom("<dependency><groupId>org.springframework.cloud</groupId><artifactId>spring-cloud-starter-netflix-zuul</artifactId></dependency>"),
            "src/main/resources/application.properties": """
                zuul.routes.inventory.path=/inventory/**
                zuul.routes.inventory.url=http://inv:8080
                zuul.routes.users.path=/api/users/**
                zuul.routes.users.serviceId=user-service
                zuul.routes.users.stripPrefix=false
                zuul.sensitive-headers=Cookie
                """,
            "src/main/java/a/App.java": """
                package a;
                import org.springframework.cloud.netflix.zuul.EnableZuulProxy;
                @EnableZuulProxy
                public class App {}
                """})
        changes, todo = sm.cm_zuul(pr.root)
        props = pr["src/main/resources/application.properties"]
        base = "spring.cloud.gateway.server.webmvc.routes"
        self.assertIn(f"{base}[0].uri=http://inv:8080", props)
        self.assertIn(f"{base}[0].predicates[0]=Path=/inventory/**", props)
        self.assertIn(f"{base}[0].filters[0]=StripPrefix=1", props)
        self.assertIn(f"{base}[0].filters[1]=RemoveRequestHeader=Cookie", props)
        self.assertIn(f"{base}[1].uri=lb://user-service", props)
        self.assertNotIn(f"{base}[1].filters[0]=StripPrefix", props)
        self.assertNotIn("zuul.", props)
        self.assertIn("spring-cloud-starter-gateway-server-webmvc", pr["pom.xml"])
        self.assertIn("spring-cloud-starter-loadbalancer", pr["pom.xml"])
        self.assertNotIn("EnableZuulProxy", pr["src/main/java/a/App.java"])
        pr.close()

    def test_ribbon_rule_and_servers(self):
        pr = Project({
            "pom.xml": pom("<dependency><groupId>org.springframework.cloud</groupId><artifactId>spring-cloud-starter-netflix-ribbon</artifactId></dependency>"),
            "src/main/resources/application.properties": "stores.ribbon.listOfServers=a:1, https://b:2\nribbon.eureka.enabled=false\n",
            "src/main/java/a/LbConfig.java": """
                package a;
                import com.netflix.loadbalancer.IRule;
                import com.netflix.loadbalancer.RandomRule;
                import org.springframework.cloud.netflix.ribbon.RibbonClient;
                import org.springframework.context.annotation.Bean;
                @RibbonClient(name = "stores", configuration = LbConfig.class)
                public class LbConfig {
                    @Bean
                    public IRule ribbonRule() { return new RandomRule(); }
                }
                """})
        sm.cm_ribbon(pr.root)
        j = pr["src/main/java/a/LbConfig.java"]
        self.assertIn("@LoadBalancerClient(name = \"stores\"", j)
        self.assertIn("new RandomLoadBalancer(", j)
        self.assertNotIn("com.netflix", j)
        props = pr["src/main/resources/application.properties"]
        self.assertIn("spring.cloud.discovery.client.simple.instances.stores[0].uri=http://a:1", props)
        self.assertIn("spring.cloud.discovery.client.simple.instances.stores[1].uri=https://b:2", props)
        self.assertNotIn("ribbon", props)
        self.assertIn("spring-cloud-starter-loadbalancer", pr["pom.xml"])
        pr.close()

    def test_hystrix_fallback_gets_throwable_and_props(self):
        pr = Project({
            "pom.xml": pom("<dependency><groupId>org.springframework.cloud</groupId><artifactId>spring-cloud-starter-netflix-hystrix</artifactId></dependency>"),
            "src/main/java/a/Svc.java": """
                package a;
                import com.netflix.hystrix.contrib.javanica.annotation.HystrixCommand;
                import com.netflix.hystrix.contrib.javanica.annotation.HystrixProperty;
                public class Svc {
                    @HystrixCommand(commandKey = "price", fallbackMethod = "fb", ignoreExceptions = IllegalArgumentException.class,
                        commandProperties = {@HystrixProperty(name = "circuitBreaker.sleepWindowInMilliseconds", value = "5000")})
                    public String price(String sku, int qty) { return "x"; }
                    public String fb(String sku, int qty) { return "y"; }
                }
                """})
        sm.cm_hystrix(pr.root)
        j = pr["src/main/java/a/Svc.java"]
        self.assertIn('@CircuitBreaker(name = "price", fallbackMethod = "fb")', j)
        self.assertIn("public String fb(String sku, int qty, Throwable throwable)", j)
        props = pr["src/main/resources/application.properties"]
        self.assertIn("resilience4j.circuitbreaker.instances.price.wait-duration-in-open-state=5000ms", props)
        self.assertIn("resilience4j.circuitbreaker.instances.price.ignore-exceptions[0]=java.lang.IllegalArgumentException", props)
        p = pr["pom.xml"]
        self.assertIn("spring-cloud-starter-circuitbreaker-resilience4j", p)
        self.assertIn("spring-cloud-dependencies", p)  # BOM added because none was present
        pr.close()


class OAuth2Test(unittest.TestCase):
    RS = """
        package a;
        import org.springframework.context.annotation.Configuration;
        import org.springframework.security.config.annotation.web.builders.HttpSecurity;
        import org.springframework.security.oauth2.config.annotation.web.configuration.EnableResourceServer;
        import org.springframework.security.oauth2.config.annotation.web.configuration.ResourceServerConfigurerAdapter;
        @Configuration
        @EnableResourceServer
        public class Rs extends ResourceServerConfigurerAdapter {
            @Override
            public void configure(HttpSecurity web) throws Exception {
                web.authorizeRequests().antMatchers("/admin/**").access("#oauth2.hasScope('admin')").anyRequest().authenticated();
            }
        }
        """
    DEP = "<dependency><groupId>org.springframework.security.oauth.boot</groupId><artifactId>spring-security-oauth2-autoconfigure</artifactId><version>2.6.8</version></dependency>"

    def test_opaque_token_info(self):
        pr = Project({"pom.xml": pom(self.DEP), "src/main/java/a/Rs.java": self.RS,
                      "src/main/resources/application.properties":
                          "security.oauth2.resource.token-info-uri=http://auth/oauth/check_token\n"
                          "security.oauth2.client.client-id=rs\nsecurity.oauth2.client.client-secret=s\n"})
        sm.cm_legacy_oauth2(pr.root)
        j = pr["src/main/java/a/Rs.java"]
        self.assertIn("oauth2.opaqueToken(Customizer.withDefaults())", j)
        self.assertIn("hasAuthority('SCOPE_admin')", j)
        self.assertIn("http.authorizeRequests()", j)  # parameter renamed to http
        props = pr["src/main/resources/application.properties"]
        self.assertIn("spring.security.oauth2.resourceserver.opaquetoken.introspection-uri=http://auth/oauth/check_token", props)
        self.assertIn("spring.security.oauth2.resourceserver.opaquetoken.client-id=rs", props)
        self.assertIn("spring-boot-starter-oauth2-resource-server", pr["pom.xml"])
        self.assertNotIn("autoconfigure", pr["pom.xml"])
        pr.close()

    def test_hmac_key(self):
        pr = Project({"pom.xml": pom(self.DEP), "src/main/java/a/Rs.java": self.RS,
                      "src/main/resources/application.yml": "security:\n  oauth2:\n    resource:\n      jwt:\n        key-value: s3cr3t\n"})
        sm.cm_legacy_oauth2(pr.root)
        j = pr["src/main/java/a/Rs.java"]
        self.assertIn("NimbusJwtDecoder.withSecretKey", j)
        self.assertIn("app.legacy-oauth2.jwt.hmac-secret: s3cr3t", pr["src/main/resources/application.yml"])
        pr.close()

    def test_sso_client(self):
        pr = Project({"pom.xml": pom(self.DEP),
                      "src/main/java/a/Web.java": """
                          package a;
                          import org.springframework.boot.autoconfigure.security.oauth2.client.EnableOAuth2Sso;
                          import org.springframework.security.config.annotation.web.builders.HttpSecurity;
                          @EnableOAuth2Sso
                          public class Web extends org.springframework.security.config.annotation.web.configuration.WebSecurityConfigurerAdapter {
                              protected void configure(HttpSecurity http) throws Exception { http.authorizeRequests().anyRequest().authenticated(); }
                          }
                          """,
                      "src/main/resources/application.properties":
                          "security.oauth2.client.client-id=web\nsecurity.oauth2.client.client-secret=s\n"
                          "security.oauth2.client.access-token-uri=http://auth/oauth/token\n"
                          "security.oauth2.client.user-authorization-uri=http://auth/oauth/authorize\n"
                          "security.oauth2.resource.user-info-uri=http://auth/me\n"})
        sm.cm_legacy_oauth2(pr.root)
        j = pr["src/main/java/a/Web.java"]
        self.assertIn("http.oauth2Login(Customizer.withDefaults());", j)
        self.assertNotIn("EnableOAuth2Sso", j)
        props = pr["src/main/resources/application.properties"]
        self.assertIn("spring.security.oauth2.client.registration.legacy.client-id=web", props)
        self.assertIn("spring.security.oauth2.client.provider.legacy.token-uri=http://auth/oauth/token", props)
        self.assertIn("spring.security.oauth2.client.registration.legacy.authorization-grant-type=authorization_code", props)
        self.assertIn("spring-boot-starter-oauth2-client", pr["pom.xml"])
        pr.close()


class JpaTest(unittest.TestCase):
    def test_jpql_positional(self):
        pr = Project({"src/main/java/a/Repo.java": """
            package a;
            public interface Repo {
                @Query("select u from User u where u.name = ? and u.city = 'a?b' and u.age > ?")
                Object find(String n, int a);
                @Query(value = "select * from users where name = ?", nativeQuery = true)
                Object nat(String n);
            }
            """, "src/main/resources/META-INF/orm.xml": "<named-query name=\"q\"><query>select x from X x where x.a = ? or x.b = ?</query></named-query>"})
        sm.cm_jpql_positional(pr.root)
        j = pr["src/main/java/a/Repo.java"]
        self.assertIn("u.name = ?1 and u.city = 'a?b' and u.age > ?2", j)
        self.assertIn("where name = ?\"", j)  # native query untouched
        self.assertIn("x.a = ?1 or x.b = ?2", pr["src/main/resources/META-INF/orm.xml"])
        pr.close()

    ENTITY = """
        package a;
        import javax.persistence.*;
        @Entity
        public class E {
            @Id
            @GeneratedValue
            private Long id;
        }
        """

    def test_pin_sequence(self):
        pr = Project({"src/main/java/a/E.java": self.ENTITY})
        sm.cm_pin_generated_ids(pr.root)
        j = pr["src/main/java/a/E.java"]
        self.assertIn('@SequenceGenerator(name = "hibernate_sequence", sequenceName = "hibernate_sequence", allocationSize = 1)', j)
        self.assertIn("GenerationType.SEQUENCE", j)
        # idempotent
        self.assertEqual(sm.cm_pin_generated_ids(pr.root)[0], [])
        pr.close()

    def test_pin_identity_for_mysql_without_new_mappings(self):
        pr = Project({"src/main/java/a/E.java": self.ENTITY.replace("Long id", "long id"),
                      "src/main/resources/application.properties":
                          "spring.datasource.url=jdbc:mysql://db/x\nspring.jpa.properties.hibernate.id.new_generator_mappings=false\n"})
        sm.cm_pin_generated_ids(pr.root)
        self.assertIn("@GeneratedValue(strategy = GenerationType.IDENTITY)", pr["src/main/java/a/E.java"])
        pr.close()

    def test_uuid_ids_untouched(self):
        pr = Project({"src/main/java/a/E.java": self.ENTITY.replace("Long id", "java.util.UUID id")})
        self.assertEqual(sm.cm_pin_generated_ids(pr.root)[0], [])
        pr.close()

    def test_openjpa_relations(self):
        pr = Project({"src/main/java/a/O.java": """
            package a;
            import java.util.List;
            import javax.persistence.*;
            import org.apache.openjpa.persistence.ElementDependent;
            import org.apache.openjpa.persistence.jdbc.ForeignKey;
            @Entity
            public class O {
                @Id Long id;
                @OneToMany(mappedBy = "o")
                @ElementDependent
                private List<L> lines;
                @ManyToOne
                @ForeignKey(name = "FK_X")
                private O parent;
            }
            """})
        _, todo = sm.cm_openjpa_annotations(pr.root)
        j = pr["src/main/java/a/O.java"]
        self.assertIn('@OneToMany(mappedBy = "o", orphanRemoval = true)', j)
        self.assertNotIn("openjpa", j)
        self.assertTrue(any("ForeignKey" in t for t in todo))
        pr.close()

    def test_dialects(self):
        pr = Project({"src/main/resources/application.properties":
                      "spring.jpa.database-platform=org.hibernate.dialect.PostgreSQL95Dialect\n"
                      "spring.jpa.properties.hibernate.id.new_generator_mappings=true\nx=1\n"})
        sm.fix_hibernate_props(pr.root)
        self.assertEqual(pr["src/main/resources/application.properties"],
                         "spring.jpa.database-platform=org.hibernate.dialect.PostgreSQLDialect\nx=1\n")
        pr.close()


class FixersTest(unittest.TestCase):
    def test_stale_pins(self):
        pr = Project({"pom.xml": pom()})
        sm.cm_stale_version_pins(pr.root)
        self.assertNotIn("mockito.version", pr["pom.xml"])
        self.assertIn("<java.version>11</java.version>", pr["pom.xml"])
        pr.close()

    def test_images_and_ci(self):
        pr = Project({"Dockerfile": "FROM maven:3.8-openjdk-11 AS b\nFROM openjdk:11-jre-slim\nFROM postgres:11\n",
                      ".gitlab-ci.yml": "image: maven:3-jdk-11\n", ".travis.yml": "jdk: openjdk11\n"})
        sm.fix_containers_and_ci(pr.root, 21)
        self.assertEqual(pr["Dockerfile"], "FROM maven:3.9-eclipse-temurin-21 AS b\nFROM eclipse-temurin:21-jre\nFROM postgres:11\n")
        self.assertIn("maven:3.9-eclipse-temurin-21", pr[".gitlab-ci.yml"])
        self.assertIn("jdk: openjdk21", pr[".travis.yml"])
        pr.close()

    def test_httptrace(self):
        pr = Project({"src/main/resources/application.properties":
                      "management.endpoints.web.exposure.include=health,httptrace\nmanagement.trace.http.enabled=true\n",
                      "src/main/java/a/T.java": "import org.springframework.boot.actuate.trace.http.InMemoryHttpTraceRepository;\n"
                                                "class T { Object r = new InMemoryHttpTraceRepository(); }\n"})
        sm.fix_httptrace(pr.root)
        props = pr["src/main/resources/application.properties"]
        self.assertIn("management.endpoints.web.exposure.include=health,httpexchanges", props)
        self.assertIn("management.httpexchanges.recording.enabled=true", props)
        self.assertIn("org.springframework.boot.actuate.web.exchanges.InMemoryHttpExchangeRepository", pr["src/main/java/a/T.java"])
        pr.close()

    def test_wrappers(self):
        pr = Project({".mvn/wrapper/maven-wrapper.properties": "distributionUrl=https://x/apache-maven-3.6.0-bin.zip\ndistributionSha256Sum=abc\n",
                      "gradle/wrapper/gradle-wrapper.properties": "distributionUrl=https\\://services.gradle.org/distributions/gradle-7.4-bin.zip\n"})
        sm.fix_build_wrappers(pr.root, "4.1")
        self.assertIn("apache-maven-3.9.11-bin", pr[".mvn/wrapper/maven-wrapper.properties"])
        self.assertNotIn("Sha256", pr[".mvn/wrapper/maven-wrapper.properties"])
        self.assertIn("gradle-8.14.3-bin", pr["gradle/wrapper/gradle-wrapper.properties"])
        pr.close()

    def test_trailing_slash_filter(self):
        pr = Project({"pom.xml": pom(), "src/main/java/a/App.java": "package a;\n@SpringBootApplication\npublic class App {}\n"})
        sm.fix_trailing_slash(pr.root)
        self.assertIn("UrlHandlerFilter.trailingSlashHandler", pr["src/main/java/a/TrailingSlashCompatibilityConfig.java"])
        self.assertEqual(sm.fix_trailing_slash(pr.root), [])
        pr.close()

    def test_boot_bump_and_cloud_train(self):
        pr = Project({"pom.xml": pom().replace("2.7.13", "4.0.10").replace("<java.version>", "<spring-cloud.version>2025.0.3</spring-cloud.version>\n    <java.version>")})
        sm.bump_boot(pr.root, "4.1.3")
        sm.fix_spring_cloud_train(pr.root, "4.1")
        self.assertIn("<version>4.1.3</version>", pr["pom.xml"])
        self.assertIn("<spring-cloud.version>2025.1.5</spring-cloud.version>", pr["pom.xml"])
        pr.close()

    def test_properties_migrator_parse(self):
        log = ("Property source 'Config resource 'class path resource [application.properties]'':\n"
               "\tKey: spring.redis.host\n\t\tLine: 3\n\t\tReplacement: spring.data.redis.host\n"
               "\tKey: server.max-http-header-size\n\t\tLine: 4\n\t\tReplacement: server.max-http-request-header-size\n"
               "The use of configuration keys that are no longer supported was found in the environment:\n"
               "\tKey: spring.foo\n\t\tLine: 5\n\t\tReason: none\n")
        renames, gone = sm.parse_properties_migrator(log)
        self.assertEqual(renames["spring.redis.host"], "spring.data.redis.host")
        self.assertEqual(gone, {"spring.foo": "none"})
        pr = Project({"src/main/resources/application.properties": "a=1\nspring.redis.host=r\n"})
        sm.apply_property_renames(pr.root, renames)
        self.assertEqual(pr["src/main/resources/application.properties"], "a=1\nspring.data.redis.host=r\n")
        pr.close()

    def test_httpclient5_parse_exception(self):
        pr = Project({"src/main/java/a/C.java": """
            package a;
            import org.apache.hc.core5.http.io.entity.EntityUtils;
            class C {
                String get(org.apache.hc.core5.http.HttpEntity e) throws java.io.IOException { return EntityUtils.toString(e); }
            }
            """})
        sm.fix_httpclient5_checked(pr.root)
        j = pr["src/main/java/a/C.java"]
        self.assertIn("return entityToString(e);", j)
        self.assertIn("catch (org.apache.hc.core5.http.ParseException e)", j)
        pr.close()


class JaxbBindingTest(unittest.TestCase):
    def test_xjc_namespace_stays_and_earlier_runs_are_repaired(self):
        xjb = ('<jaxb:bindings xmlns:jaxb="http://java.sun.com/xml/ns/jaxb" xmlns:xjc="http://java.sun.com/xml/ns/jaxb/xjc" '
               'jaxb:extensionBindingPrefixes="xjc" version="2.1">\n  <jaxb:globalBindings><xjc:javaType name="java.time.LocalDate" '
               'xmlType="xs:date" adapter="a.B"/></jaxb:globalBindings>\n</jaxb:bindings>\n')
        jaxws = '<jaxws:bindings xmlns:jaxws="http://java.sun.com/xml/ns/jaxws" wsdlLocation="UrvService.wsdl"/>\n'
        broken = xjb.replace("http://java.sun.com/xml/ns/jaxb/xjc", "https://jakarta.ee/xml/ns/jaxb/xjc")
        pr = Project({"src/main/resources/binding/datetime.xjb.xml": xjb, "src/main/resources/binding/jaxws.binding.xml": jaxws,
                      "src/main/resources/binding/old.xjb": broken})
        sm.fix_jaxb_tooling(pr.root)
        for name in ("datetime.xjb.xml", "old.xjb"):
            out = pr["src/main/resources/binding/" + name]
            self.assertIn('xmlns:jaxb="https://jakarta.ee/xml/ns/jaxb"', out)
            self.assertIn('xmlns:xjc="http://java.sun.com/xml/ns/jaxb/xjc"', out)   # XJC namespace did not move
            self.assertIn('version="3.0"', out)
        self.assertIn('xmlns:jaxws="https://jakarta.ee/xml/ns/jaxws"', pr["src/main/resources/binding/jaxws.binding.xml"])
        self.assertEqual(sm.fix_jaxb_tooling(pr.root)[0], [])                       # idempotent
        pr.close()


class JaxbVersionAndPluginDepsTest(unittest.TestCase):
    def test_versions_bumped_in_jaxws_files_and_inline_customizations(self):
        jaxws = ('<jaxws:bindings xmlns:jaxws="http://java.sun.com/xml/ns/jaxws" xmlns:jaxb="http://java.sun.com/xml/ns/jaxb" '
                 'version="2.1" wsdlLocation="S.wsdl"><jaxb:bindings node="//xs:schema"><jaxb:globalBindings/></jaxb:bindings></jaxws:bindings>\n')
        wsdl = ('<definitions xmlns="http://schemas.xmlsoap.org/wsdl/" xmlns:jaxb="http://java.sun.com/xml/ns/jaxb" '
                'xmlns:xjc="http://java.sun.com/xml/ns/jaxb/xjc"><types><xs:schema jaxb:version="2.1" jaxb:extensionBindingPrefixes="xjc"/></types></definitions>\n')
        pr = Project({"src/main/resources/ws/jaxws.binding.xml": jaxws, "src/main/resources/ws/S.wsdl": wsdl})
        sm.fix_jaxb_tooling(pr.root)
        j, w = pr["src/main/resources/ws/jaxws.binding.xml"], pr["src/main/resources/ws/S.wsdl"]
        self.assertIn('version="3.0"', j)
        self.assertIn('xmlns:jaxws="https://jakarta.ee/xml/ns/jaxws"', j)
        self.assertIn('jaxb:version="3.0"', w)
        self.assertIn('xmlns:jaxb="https://jakarta.ee/xml/ns/jaxb"', w)
        self.assertIn('xmlns:xjc="http://java.sun.com/xml/ns/jaxb/xjc"', w)
        pr.close()

    def test_plugin_dependencies_are_planned(self):
        pr = Project({"pom.xml": """<project><groupId>at.acme</groupId><artifactId>app</artifactId>
  <build><plugins><plugin><groupId>org.codehaus.mojo</groupId><artifactId>exec-maven-plugin</artifactId>
    <dependencies><dependency><groupId>at.acme.spring</groupId><artifactId>commons-lang</artifactId><version>1.48.29</version></dependency></dependencies>
  </plugin></plugins></build></project>"""})
        refs = {(r.group, r.artifact, r.version) for r in sm.collect_dependency_refs(pr.root)}
        self.assertIn(("at.acme.spring", "commons-lang", "1.48.29"), refs)
        plan = {"dependencies": [{"coordinate": "at.acme.spring:commons-lang", "upgrades": {"3.5": "2.7.4"}}]}
        sm.apply_dependency_plan(pr.root, plan, "3.5")
        self.assertIn("<artifactId>commons-lang</artifactId><version>2.7.4</version>", pr["pom.xml"])
        pr.close()


class DowngradedBootManagedTest(unittest.TestCase):
    def test_older_explicit_versions_yield_to_boot(self):
        with tempfile.TemporaryDirectory() as t:
            local = Path(t) / "repo"
            bom = local / "org/springframework/boot/spring-boot-dependencies/3.5.16/spring-boot-dependencies-3.5.16.pom"
            bom.parent.mkdir(parents=True)
            bom.write_text("<project><properties><logback.version>1.5.34</logback.version><slf4j.version>2.0.18</slf4j.version></properties>"
                           "<dependencyManagement><dependencies>"
                           "<dependency><groupId>ch.qos.logback</groupId><artifactId>logback-classic</artifactId><version>${logback.version}</version></dependency>"
                           "<dependency><groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId><version>${slf4j.version}</version></dependency>"
                           "</dependencies></dependencyManagement></project>")
            pr = Project({"pom.xml": """<project><properties><old.slf4j>1.7.36</old.slf4j></properties>
  <dependencyManagement><dependencies>
    <dependency><groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId><version>${old.slf4j}</version></dependency>
  </dependencies></dependencyManagement>
  <dependencies>
    <dependency><groupId>ch.qos.logback</groupId><artifactId>logback-classic</artifactId><version>1.2.13</version></dependency>
    <dependency><groupId>ch.qos.logback</groupId><artifactId>logback-core</artifactId><version>1.2.13</version></dependency>
  </dependencies>
  <build><plugins><plugin><artifactId>exec-maven-plugin</artifactId><dependencies>
    <dependency><groupId>ch.qos.logback</groupId><artifactId>logback-classic</artifactId><version>1.2.13</version></dependency>
  </dependencies></plugin></plugins></build>
</project>"""})
            changes, todo = sm.fix_downgraded_boot_managed(pr.root, "3.5.16", local)
            pom = pr["pom.xml"]
            self.assertIn("<artifactId>slf4j-api</artifactId></dependency>", pom)                   # managed entry dropped its version
            self.assertIn("<artifactId>logback-classic</artifactId></dependency>", pom)             # dependency: Boot's version
            self.assertIn("<artifactId>logback-classic</artifactId><version>1.5.34</version>", pom) # plugin dependency: set
            self.assertIn("<artifactId>logback-core</artifactId><version>1.2.13</version>", pom)    # not managed here: untouched
            self.assertEqual(len(changes), 3)
            self.assertEqual(sm.fix_downgraded_boot_managed(pr.root, "3.5.16", local)[0], [])        # idempotent
            pr.close()


class ManagementGapTest(unittest.TestCase):
    POM = """<project>
  <dependencies>
    <dependency>
      <groupId>org.jvnet.jaxb2_commons</groupId>
      <artifactId>jaxb2-basics-runtime</artifactId>
    </dependency>
  </dependencies>
  <build><plugins><plugin>
    <groupId>com.sun.xml.ws</groupId><artifactId>jaxws-maven-plugin</artifactId>
    <dependencies>
      <dependency><groupId>org.jvnet.jaxb2_commons</groupId><artifactId>jaxb2-basics</artifactId></dependency>
    </dependencies>
  </plugin></plugins></build>
</project>
"""

    def test_missing_version_messages_are_parsed(self):
        m = sm._MISSING_RX.search("[ERROR]     'build.plugins.plugin[com.sun.xml.ws:jaxws-maven-plugin].dependencies.dependency.version' "
                                  "for org.jvnet.jaxb2_commons:jaxb2-basics:jar is missing. @ line 241, column 21")
        self.assertEqual(m.groups(), ("com.sun.xml.ws", "jaxws-maven-plugin", "org.jvnet.jaxb2_commons", "jaxb2-basics", "241"))
        m = sm._MISSING_RX.search("'dependencies.dependency.version' for net.sourceforge.plantuml:plantuml-mit:jar is missing. @ line 251, column 21")
        self.assertEqual(m.group(3, 4), ("net.sourceforge.plantuml", "plantuml-mit"))

    def test_pins_dependency_and_plugin_dependency(self):
        out = sm._pin_version(self.POM, "org.jvnet.jaxb2_commons", "jaxb2-basics-runtime", "1.11.1", None, 3)
        out = sm._pin_version(out, "org.jvnet.jaxb2_commons", "jaxb2-basics", "1.11.1", "jaxws-maven-plugin", None)
        self.assertIn("<artifactId>jaxb2-basics-runtime</artifactId>\n      <version>1.11.1</version>", out)
        self.assertIn("<artifactId>jaxb2-basics</artifactId><version>1.11.1</version>", out)


class EncodingTest(unittest.TestCase):
    def test_latin1_properties_keep_their_encoding(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            props = root / "src/main/resources/application.properties"
            props.parent.mkdir(parents=True)
            original = "app.greeting=Grüß Gott, Österreich\nmanagement.trace.http.enabled=true\n"
            props.write_bytes(original.encode("latin-1"))
            import subprocess
            g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=root, capture_output=True)
            g("init", "-q"); g("add", "-A"); g("commit", "-qm", "base")
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
            props.write_bytes((original + 'springdoc.api-docs.path="/v3/api-docs"\n').encode("latin-1"))
            sm.fix_quoted_properties(root, base)          # used to crash: git show output decoded as UTF-8
            sm.fix_httptrace(root)                        # edits the file
            data = props.read_bytes()
            text = data.decode("latin-1")
            self.assertIn("Grüß Gott, Österreich", text)   # umlauts intact, still ISO-8859-1
            self.assertRaises(UnicodeDecodeError, data.decode, "utf-8")
            self.assertIn("springdoc.api-docs.path=/v3/api-docs", text)
            self.assertIn("management.httpexchanges.recording.enabled=true", text)

    def test_utf8_files_stay_utf8(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "A.java"
            p.write_text("// Größe\nclass A {}\n", encoding="utf-8")
            sm.write_file(p, sm.read(p).replace("class A", "class B"))
            self.assertEqual(p.read_text(encoding="utf-8"), "// Größe\nclass B {}\n")


class ImportOrderTest(unittest.TestCase):
    def test_inserted_in_sorted_position_and_group(self):
        src = ("package a;\n\nimport java.util.List;\nimport java.util.Map;\n\n"
               "import org.springframework.context.annotation.Bean;\nimport org.springframework.stereotype.Service;\n\nclass A {}\n")
        out = sm.add_import(src, "org.springframework.context.annotation.Configuration")
        self.assertIn("annotation.Bean;\nimport org.springframework.context.annotation.Configuration;\nimport org.springframework.stereotype", out)
        out = sm.add_import(out, "java.util.ArrayList")
        self.assertIn("import java.util.ArrayList;\nimport java.util.List;", out)
        out = sm.add_import(out, "org.springframework.web.filter.UrlHandlerFilter")
        self.assertIn("stereotype.Service;\nimport org.springframework.web.filter.UrlHandlerFilter;\n\nclass", out)


class SpringdocTest(unittest.TestCase):
    ROOT = """<project>
  <parent><groupId>{pg}</groupId><artifactId>{pa}</artifactId><version>1</version></parent>
  <groupId>com.acme</groupId><artifactId>root</artifactId><version>1</version><packaging>pom</packaging>
  <modules><module>web</module></modules>
  <properties><springdoc.version>1.7.0</springdoc.version></properties>
  {dm}
</project>
"""
    WEB = ("<project><parent><groupId>com.acme</groupId><artifactId>root</artifactId><version>1</version></parent><artifactId>web</artifactId>"
           "<dependencies><dependency><groupId>org.springdoc</groupId><artifactId>springdoc-openapi-ui</artifactId></dependency>"
           "<dependency><groupId>org.springdoc</groupId><artifactId>springdoc-openapi-security</artifactId></dependency></dependencies></project>")
    JAVA = ("package a;\nimport org.springdoc.core.models.GroupedOpenApi;\nimport org.springdoc.core.customizers.OpenApiCustomizer;\n"
            "class C { Object g(OpenApiCustomizer c) { return GroupedOpenApi.builder().addOpenApiCustomizer(c).build(); } }\n")
    DM = ("<dependencyManagement><dependencies><dependency><groupId>org.springdoc</groupId><artifactId>springdoc-openapi-ui</artifactId>"
          "<version>${springdoc.version}</version></dependency></dependencies></dependencyManagement>")

    def project(self, dm=True, parent=("org.springframework.boot", "spring-boot-starter-parent")):
        return Project({"pom.xml": self.ROOT.format(pg=parent[0], pa=parent[1], dm=self.DM if dm else ""), "web/pom.xml": self.WEB,
                        "web/src/main/java/a/C.java": self.JAVA})

    def test_boot27_stage_keeps_springdoc1_names(self):
        pr = self.project()
        sm.fix_springdoc(pr.root, "2.7")
        j = pr["web/src/main/java/a/C.java"]
        self.assertIn("import org.springdoc.core.GroupedOpenApi;", j)
        self.assertIn("OpenApiCustomiser", j)
        self.assertIn("addOpenApiCustomiser", j)
        self.assertIn("<springdoc.version>1.7.0</springdoc.version>", pr["pom.xml"])   # already a 1.x version
        pr.close()

    def test_boot35_upgrades_root_managed_property_and_artifacts(self):
        pr = self.project()
        sm.fix_springdoc(pr.root, "3.5")
        root, web = pr["pom.xml"], pr["web/pom.xml"]
        self.assertIn("<springdoc.version>2.8.17</springdoc.version>", root)
        self.assertIn("springdoc-openapi-starter-webmvc-ui", root)
        self.assertIn("springdoc-openapi-starter-webmvc-ui", web)
        self.assertNotIn("springdoc-openapi-security", web)                 # merged into springdoc 2
        self.assertIn("import org.springdoc.core.models.GroupedOpenApi;", pr["web/src/main/java/a/C.java"])
        self.assertEqual(sm.fix_springdoc(pr.root, "3.5"), [])            # idempotent
        pr.close()

    def test_version_from_company_parent_is_pinned_in_root(self):
        pr = self.project(dm=False, parent=("com.acme.platform", "acme-parent"))
        sm.fix_springdoc(pr.root, "4.1")
        root = pr["pom.xml"]
        self.assertIn("<artifactId>springdoc-openapi-starter-webmvc-ui</artifactId>\n        <version>3.0.3</version>", root)
        pr.close()


class BuilderTest(unittest.TestCase):
    def capture(self, **kw):
        seen = []
        old = sm.run
        sm.run = lambda cmd, cwd, log, env: seen.append(cmd) or 0
        try:
            b = sm.Builder(Path("."), "maven", {}, Path(tempfile.mkdtemp()), ["-s", "corp.xml"], None, **kw)
            b.rewrite("s", ["R"], ["a:b:1"], dry=False)
            b.verify("s")
        finally:
            sm.run = old
        return seen

    def test_frontend_skipped_only_for_openrewrite_by_default(self):
        rewrite, verify = self.capture()
        self.assertIn("-Dskip.npm", rewrite)
        self.assertIn("-Dskip.installnodenpm", rewrite)
        self.assertNotIn("-Dskip.npm", verify)          # your build gates still build the frontend
        self.assertIn("-Dspotless.check.skip=true", rewrite)   # no format/style gate while rewriting
        self.assertIn("-Dcheckstyle.skip=true", rewrite)
        self.assertNotIn("-Dspotless.check.skip=true", verify)  # ... but the build gate keeps them
        self.assertIn("corp.xml", rewrite)
        self.assertTrue(rewrite[4].endswith(":run"))

    def test_skip_frontend_everywhere_and_no_fork(self):
        rewrite, verify = self.capture(skip_frontend=True, no_fork=True, rewrite_extra=["-Ddocker.skip"])
        self.assertIn("-Dskip.npm", verify)
        self.assertEqual(rewrite.count("-Dskip.npm"), 1)
        self.assertTrue(rewrite[4].endswith(":runNoFork"))
        self.assertIn("-Ddocker.skip", rewrite)
        self.assertNotIn("-Ddocker.skip", verify)

    def test_spotless_only_on_changed_files(self):
        seen = []
        old = sm.run
        log_dir = Path(tempfile.mkdtemp())
        def fake(cmd, cwd, log, env):
            seen.append(cmd)
            log.write_text("[ERROR] No plugin found for prefix 'spotless' in the current project" if len(seen) > 1 else "ok")
            return 0 if len(seen) == 1 else 1
        sm.run = fake
        try:
            b = sm.Builder(Path("."), "maven", {}, log_dir, [], None)
            self.assertEqual(b.format("s", ["web/src/main/java/a/B.java"]), ["formatted 1 changed file(s) with `spotless:apply`"])
            self.assertIn("-DspotlessFiles=.*/web/src/main/java/a/B\\.java", seen[0])
            self.assertIsNone(b.format("s2", ["x/Y.java"]))          # no Spotless in this build
            self.assertEqual(b.format("s3", ["x/Y.java"]), [])       # ... and not tried again
            self.assertEqual(len(seen), 2)
        finally:
            sm.run = old

    def test_rewrite_frontend_keeps_npm(self):
        rewrite, _ = self.capture(rewrite_frontend=True)
        self.assertNotIn("-Dskip.npm", rewrite)


class MultiModuleTest(unittest.TestCase):
    ROOT = """
        <project>
          <parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId><version>2.7.13</version></parent>
          <groupId>com.acme.shop</groupId><artifactId>shop-parent</artifactId><version>1.0.0</version><packaging>pom</packaging>
          <modules><module>core</module><module>web</module><module>worker</module></modules>
          <properties><java.version>11</java.version></properties>
          <dependencyManagement><dependencies>
            <dependency><groupId>com.acme.shop</groupId><artifactId>shop-core</artifactId><version>1.0.0</version></dependency>
            <dependency><groupId>org.springframework.cloud</groupId><artifactId>spring-cloud-starter-netflix-hystrix</artifactId><version>2.2.10.RELEASE</version></dependency>
          </dependencies></dependencyManagement>
        </project>
        """
    CHILD = """
        <project>
          <parent><groupId>com.acme.shop</groupId><artifactId>shop-parent</artifactId><version>1.0.0</version></parent>
          <artifactId>{a}</artifactId>
          <dependencies>{deps}</dependencies>
        </project>
        """

    def project(self):
        web = '<dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency>'
        hx = '<dependency><groupId>org.springframework.cloud</groupId><artifactId>spring-cloud-starter-netflix-hystrix</artifactId></dependency>'
        return Project({
            "pom.xml": self.ROOT,
            "core/pom.xml": self.CHILD.format(a="shop-core", deps=""),
            "web/pom.xml": self.CHILD.format(a="web", deps=web + '<dependency><groupId>com.acme.shop</groupId><artifactId>shop-core</artifactId><version>1.0.0</version></dependency>'),
            "worker/pom.xml": self.CHILD.format(a="worker", deps=hx),
            "web/src/main/java/a/web/WebApp.java": "package a.web;\n@SpringBootApplication\npublic class WebApp {}\n",
            "worker/src/main/java/a/worker/WorkerApp.java": "package a.worker;\nimport com.netflix.hystrix.contrib.javanica.annotation.HystrixCommand;\n"
                "@SpringBootApplication\npublic class WorkerApp {\n    @HystrixCommand(fallbackMethod = \"fb\")\n    public int x() { return 1; }\n"
                "    public int fb() { return 2; }\n}\n",
        })

    def test_own_modules_are_not_dependencies(self):
        pr = self.project()
        refs = {(r.group, r.artifact) for r in sm.collect_dependency_refs(pr.root)}
        self.assertNotIn(("com.acme.shop", "shop-parent"), refs)     # the children's parent is the build itself
        self.assertNotIn(("com.acme.shop", "shop-core"), refs)       # sibling module
        self.assertEqual([p.parent.name for p in sm.reactor_ancestors(pr.root, pr.root / "worker/pom.xml")], ["worker", pr.root.name])
        pr.close()

    def test_hystrix_in_child_cleans_root_management_and_puts_bom_in_root(self):
        pr = self.project()
        sm.cm_hystrix(pr.root)
        root, worker = pr["pom.xml"], pr["worker/pom.xml"]
        self.assertNotIn("hystrix", root)                                  # managed entry removed
        self.assertIn("spring-cloud-dependencies", root)                   # BOM added once, at the top
        self.assertNotIn("spring-cloud-dependencies", worker)
        self.assertIn("spring-cloud-starter-circuitbreaker-resilience4j", worker)
        self.assertNotIn("resilience4j", root)                             # not forced on every module
        self.assertNotIn("resilience4j", pr["web/pom.xml"])
        pr.close()

    def test_trailing_slash_only_for_web_apps(self):
        pr = self.project()
        sm.fix_trailing_slash(pr.root)
        self.assertTrue(pr.exists("web/src/main/java/a/web/TrailingSlashCompatibilityConfig.java"))
        self.assertFalse(pr.exists("worker/src/main/java/a/worker/TrailingSlashCompatibilityConfig.java"))
        pr.close()


class CompanyDependenciesTest(unittest.TestCase):
    """Classification works on files in the local Maven repo; MavenRepo.resolve is replaced by a no-op."""

    def repo(self, files: dict) -> "sm.MavenRepo":
        self.tmp = tempfile.TemporaryDirectory()
        local = Path(self.tmp.name)
        import zipfile
        for name, content in files.items():
            p = local / name
            p.parent.mkdir(parents=True, exist_ok=True)
            if name.endswith(".jar"):
                with zipfile.ZipFile(p, "w") as z:
                    for entry, data in content.items():
                        z.writestr(entry, data)
            else:
                p.write_text(content, encoding="utf-8")
        r = sm.MavenRepo(Path("."), "mvn", [], {}, local)
        r._local = local
        r.resolve = lambda items, name, update=False: None
        return r

    @staticmethod
    def pom(g, a, v, parent=None, deps=""):
        par = f"<parent><groupId>{parent[0]}</groupId><artifactId>{parent[1]}</artifactId><version>{parent[2]}</version></parent>" if parent else ""
        return f"<project>{par}<groupId>{g}</groupId><artifactId>{a}</artifactId><version>{v}</version><dependencies>{deps}</dependencies></project>"

    def test_classification_and_plan(self):
        boot = "org.springframework.boot", "spring-boot-starter-parent"
        files = {
            "com/acme/acme-parent/maven-metadata-company.xml": "<versions><version>1.0</version><version>2.0</version><version>3.0</version></versions>",
            "com/acme/acme-parent/1.0/acme-parent-1.0.pom": self.pom("com.acme", "acme-parent", "1.0", (*boot, "2.7.18")),
            "com/acme/acme-parent/2.0/acme-parent-2.0.pom": self.pom("com.acme", "acme-parent", "2.0", (*boot, "3.5.16")),
            "com/acme/acme-parent/3.0/acme-parent-3.0.pom": self.pom("com.acme", "acme-parent", "3.0", (*boot, "4.1.3")),
            # a starter without Boot in its POM: classified from the jar (javax + spring.factories -> Boot 2)
            "com/acme/web-lib/maven-metadata-company.xml": "<versions><version>1.1</version><version>1.5</version></versions>",
            "com/acme/web-lib/1.1/web-lib-1.1.pom": self.pom("com.acme", "web-lib", "1.1"),
            "com/acme/web-lib/1.1/web-lib-1.1.jar": {"META-INF/spring.factories": "org.springframework.boot.autoconfigure.EnableAutoConfiguration=a.B",
                                                    "a/B.class": b"\xca\xfe javax/servlet/Filter"},
            "com/acme/web-lib/1.5/web-lib-1.5.pom": self.pom("com.acme", "web-lib", "1.5"),
            "com/acme/web-lib/1.5/web-lib-1.5.jar": {"META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports": "a.B",
                                                    "a/B.class": b"\xca\xfe jakarta/servlet/Filter"},
            # only ever built for Boot 2
            "com/acme/old-lib/maven-metadata-company.xml": "<versions><version>0.9</version></versions>",
            "com/acme/old-lib/0.9/old-lib-0.9.pom": self.pom("com.acme", "old-lib", "0.9", ("com.acme", "acme-parent", "1.0")),
            # plain utility library: no Spring coupling
            "com/acme/util/maven-metadata-company.xml": "<versions><version>4.0</version></versions>",
            "com/acme/util/4.0/util-4.0.pom": self.pom("com.acme", "util", "4.0"),
            "com/acme/util/4.0/util-4.0.jar": {"u/U.class": b"\xca\xfe java/lang/String"},
        }
        r = self.repo(files)
        pr = Project({"pom.xml": """
            <project>
              <parent><groupId>com.acme</groupId><artifactId>acme-parent</artifactId><version>1.0</version></parent>
              <groupId>com.acme</groupId><artifactId>shop</artifactId>
              <properties><web.version>1.1</web.version></properties>
              <dependencies>
                <dependency><groupId>com.acme</groupId><artifactId>web-lib</artifactId><version>${web.version}</version></dependency>
                <dependency><groupId>com.acme</groupId><artifactId>old-lib</artifactId><version>0.9</version></dependency>
                <dependency><groupId>com.acme</groupId><artifactId>util</artifactId><version>4.0</version></dependency>
                <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency>
              </dependencies>
            </project>
            """})
        plan = sm.detect_dependency_plan(pr.root, r, "4.1", [], say_=lambda m: None)
        by = {d["coordinate"]: d for d in plan["dependencies"]}
        self.assertEqual(by["com.acme:acme-parent"]["upgrades"], {"3.5": "2.0", "4.0": "3.0", "4.1": "3.0"})
        self.assertEqual(by["com.acme:web-lib"]["upgrades"]["3.5"], "1.5")
        self.assertIn("spring.factories", by["com.acme:web-lib"]["builtAgainst"]["1.1"])
        self.assertTrue(by["com.acme:old-lib"]["status"].startswith("NO compatible version"))
        self.assertTrue(by["com.acme:util"]["status"].startswith("no Spring"))
        self.assertNotIn("org.springframework.boot:spring-boot-starter-web", by)

        sm.apply_dependency_plan(pr.root, plan, "3.5")
        pom = pr["pom.xml"]
        self.assertIn("<web.version>1.5</web.version>", pom)
        self.assertIn("<artifactId>acme-parent</artifactId><version>2.0</version>", pom)
        sm.apply_dependency_plan(pr.root, plan, "4.1")
        self.assertIn("<artifactId>acme-parent</artifactId><version>3.0</version>", pr["pom.xml"])
        pr.close()
        self.tmp.cleanup()

    def test_openjpa_and_hibernate_libraries(self):
        boot = "org.springframework.boot", "spring-boot-starter-parent"
        dep = lambda g, a, v="": f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId>{f'<version>{v}</version>' if v else ''}</dependency>"
        files = {
            "com/acme/persistence/maven-metadata-company.xml":
                "<versions><version>1.0</version><version>1.1</version><version>2.0</version></versions>",
            # 1.0: built on OpenJPA (enhanced entity -> PersistenceCapable in the bytecode)
            "com/acme/persistence/1.0/persistence-1.0.pom": self.pom("com.acme", "persistence", "1.0", (*boot, "2.7.18"),
                                                                     dep("org.apache.openjpa", "openjpa", "3.2.2")),
            "com/acme/persistence/1.0/persistence-1.0.jar": {"p/E.class": b"\xca\xfe org/apache/openjpa/enhance/PersistenceCapable javax/persistence/Entity"},
            # 1.1: same Boot 2.7 / javax line, but on Hibernate 5
            "com/acme/persistence/1.1/persistence-1.1.pom": self.pom("com.acme", "persistence", "1.1", (*boot, "2.7.18"),
                                                                     dep("org.hibernate", "hibernate-core")),
            "com/acme/persistence/1.1/persistence-1.1.jar": {"p/E.class": b"\xca\xfe org/hibernate/Session javax/persistence/Entity"},
            # 2.0: Boot 3.5, Hibernate 6, calls Hibernate APIs
            "com/acme/persistence/2.0/persistence-2.0.pom": self.pom("com.acme", "persistence", "2.0", (*boot, "3.5.16"),
                                                                     dep("org.hibernate.orm", "hibernate-core")),
            "com/acme/persistence/2.0/persistence-2.0.jar": {"p/E.class": b"\xca\xfe org/hibernate/Session jakarta/persistence/Entity"},
        }
        r = self.repo(files)
        pr = Project({"pom.xml": """
            <project>
              <parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId><version>2.7.18</version></parent>
              <groupId>com.acme</groupId><artifactId>app</artifactId>
              <dependencies>
                <dependency><groupId>com.acme</groupId><artifactId>persistence</artifactId><version>1.0</version></dependency>
              </dependencies>
            </project>
            """})
        plan = sm.detect_dependency_plan(pr.root, r, "4.1", [], say_=lambda m: None)
        d = plan["dependencies"][0]
        self.assertTrue(plan["openjpaViaDependencies"])
        self.assertEqual(plan["lines"][0], sm.HIBERNATE_LINE)
        self.assertTrue(d["usesOpenJPA"])
        self.assertIn("OpenJPA", d["builtAgainst"]["1.0"])
        self.assertIn("Hibernate 5", d["builtAgainst"]["1.1"])
        # OpenJPA stage gets the Hibernate-based 2.7 build, Boot 3.5 gets 2.0; nothing was built for Boot 4
        self.assertEqual(d["upgrades"].get(sm.HIBERNATE_LINE), "1.1")
        self.assertEqual(d["upgrades"].get("3.5"), "2.0")
        self.assertNotIn("4.0", d["upgrades"])
        self.assertIn("nothing built for Boot 4.0, 4.1", d["status"])
        sm.apply_dependency_plan(pr.root, plan, sm.HIBERNATE_LINE)
        self.assertIn("<version>1.1</version>", pr["pom.xml"])
        pr.close()
        self.tmp.cleanup()

    def test_hibernate6_library_on_boot4_is_flagged(self):
        boot = "org.springframework.boot", "spring-boot-starter-parent"
        files = {
            "com/acme/repo/maven-metadata-company.xml": "<versions><version>1.0</version><version>2.0</version></versions>",
            "com/acme/repo/1.0/repo-1.0.pom": self.pom("com.acme", "repo", "1.0", (*boot, "3.5.16")),
            "com/acme/repo/1.0/repo-1.0.jar": {"p/R.class": b"\xca\xfe org/hibernate/Session jakarta/persistence/Entity"},
            # 2.0: jakarta, no Boot in the POM (line unknown), built against Hibernate 6
            "com/acme/repo/2.0/repo-2.0.pom": self.pom("com.acme", "repo", "2.0", None,
                                                       "<dependency><groupId>org.hibernate.orm</groupId><artifactId>hibernate-core</artifactId><version>6.6.1.Final</version></dependency>"),
            "com/acme/repo/2.0/repo-2.0.jar": {"p/R.class": b"\xca\xfe org/hibernate/Session jakarta/persistence/Entity"},
        }
        r = self.repo(files)
        pr = Project({"pom.xml": "<project><parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId>"
                                 "<version>3.5.16</version></parent><groupId>com.acme</groupId><artifactId>app</artifactId><dependencies>"
                                 "<dependency><groupId>com.acme</groupId><artifactId>repo</artifactId><version>1.0</version></dependency>"
                                 "</dependencies></project>"})
        d = sm.detect_dependency_plan(pr.root, r, "4.1", [], say_=lambda m: None)["dependencies"][0]
        self.assertEqual(d["upgrades"].get("4.1"), "2.0")
        self.assertIn("built for Hibernate 6", d["status"])
        pr.close()
        self.tmp.cleanup()

    def test_local_repository_and_slow_repositories(self):
        pr = Project({"settings.xml": "<settings><localRepository>${user.home}/corp-m2</localRepository></settings>",
                      "build.log": "[WARNING] Could not transfer metadata com.acme:x/maven-metadata.xml from/to central "
                                   "(https://repo.maven.apache.org/maven2): Connect timed out\n"
                                   "[INFO] Downloaded from company: ...\n"})
        self.assertEqual(sm.local_repository(["-s", str(pr.root / "settings.xml")], {}), Path.home() / "corp-m2")
        self.assertEqual(sm.local_repository(["-Dmaven.repo.local=/x/repo"], {}), Path("/x/repo"))
        probs = sm.repository_problems(pr.root / "build.log")
        self.assertEqual(list(probs), ["central (https://repo.maven.apache.org/maven2)"])
        self.assertIn("timed out", probs["central (https://repo.maven.apache.org/maven2)"])
        pr.close()

    def test_interactive_choice_and_gradle(self):
        plan = {"target": "3.5", "dependencies": [
            {"coordinate": "com.acme:lib", "kind": "dependency", "current": "1.0", "status": "upgrade",
             "builtAgainst": {"1.0": "Boot 2.7", "2.0": "Boot 3.5"}, "upgrades": {"3.5": "2.0"}}]}
        answers = iter(["2.1"])
        sm.interactive_plan(plan, inp=lambda prompt: next(answers), out=lambda *x: None)
        self.assertEqual(plan["dependencies"][0]["upgrades"]["3.5"], "2.1")
        pr = Project({"build.gradle": "dependencies {\n    implementation 'com.acme:lib:1.0'\n}\n"})
        sm.apply_dependency_plan(pr.root, plan, "3.5")
        self.assertIn("'com.acme:lib:2.1'", pr["build.gradle"])
        pr.close()


if __name__ == "__main__":
    unittest.main()
