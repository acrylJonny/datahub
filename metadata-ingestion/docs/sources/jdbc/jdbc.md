### Starter Recipe

```yaml
source:
  type: jdbc
  config:
    # JDBC Driver Configuration
    driver:
      driver_class: org.postgresql.Driver  # Replace with your database's driver class
      # Either specify maven_coordinates or driver_path
      maven_coordinates: org.postgresql:postgresql:42.7.1
      # driver_path: "/path/to/driver.jar"

    # Connection Configuration
    connection:
      uri: "jdbc:postgresql://localhost:5432//mydb"  # Replace with your database URI
      username: "user"
      password: "pass"
      
      # Optional SSL Configuration
      ssl_config:
        cert_path: "/path/to/cert"
        # cert_type: "pem"    # pem, jks, or p12
        # cert_password: ""
      
      # Additional JDBC Properties
      properties:
        applicationName: "datahub_jdbc_ingestion"
      
    # Additional JVM Arguments
    jvm_args:
      - "-Xmx1g"
      - "-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts"
        
    # Optional: SQL dialect for query parsing
    sqlglot_dialect: "postgres"  # Replace with your database's dialect

    # Optional Filters
    schema_pattern:
      allow:
        - "schema1"
        - "schema2"
    
    table_pattern:
      allow:
        - "schema1.table1"
      deny:
        - "schema1.temp_.*"

    # Feature flags
    include_tables: true
    include_views: true
    include_stored_procedures: false

sink:
  # sink configs
```