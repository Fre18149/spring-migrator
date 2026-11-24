package com.acme.legacy;

import java.net.URL;
import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.GeneratedValue;
import javax.persistence.Id;
import javax.persistence.NamedQuery;
import javax.persistence.Version;
import org.apache.openjpa.persistence.DataCache;
import org.apache.openjpa.persistence.Externalizer;
import org.apache.openjpa.persistence.FetchAttribute;
import org.apache.openjpa.persistence.FetchGroup;
import org.apache.openjpa.persistence.FetchGroups;
import org.apache.openjpa.persistence.Persistent;
import org.apache.openjpa.persistence.ReadOnly;
import org.apache.openjpa.persistence.UpdateAction;
import org.apache.openjpa.persistence.jdbc.Index;

@Entity
@DataCache(enabled = false)
@FetchGroups({
    @FetchGroup(name = "withWebsite", attributes = {@FetchAttribute(name = "website")})
})
@NamedQuery(name = "Account.byOwner", query = "select a from Account a where a.ownerName = :owner")
public class Account {
    @Id
    @GeneratedValue
    private Long id;
    @Index(name = "IDX_ACCOUNT_OWNER")
    @Column(name = "OWNER_NAME")
    private String ownerName;
    private long balanceCents;
    @Persistent
    @Externalizer("toExternalForm")
    private URL website;
    @ReadOnly(UpdateAction.IGNORE)
    @Column(name = "OPENED_BY")
    private String openedBy = "system";
    @Version
    private int version;

    protected Account() {}
    public Account(String ownerName, long balanceCents) { this.ownerName = ownerName; this.balanceCents = balanceCents; }
    public Long getId() { return id; }
    public String getOwnerName() { return ownerName; }
    public long getBalanceCents() { return balanceCents; }
    public URL getWebsite() { return website; }
    public void setWebsite(URL website) { this.website = website; }
    public String getOpenedBy() { return openedBy; }
}
