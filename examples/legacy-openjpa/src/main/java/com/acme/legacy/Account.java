package com.acme.legacy;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.GeneratedValue;
import javax.persistence.Id;
import javax.persistence.NamedQuery;
import javax.persistence.Version;

@Entity
@NamedQuery(name = "Account.byOwner", query = "select a from Account a where a.ownerName = :owner")
public class Account {
    @Id
    @GeneratedValue
    private Long id;
    @Column(name = "OWNER_NAME")
    private String ownerName;
    private long balanceCents;
    @Version
    private int version;

    protected Account() {}
    public Account(String ownerName, long balanceCents) { this.ownerName = ownerName; this.balanceCents = balanceCents; }
    public Long getId() { return id; }
    public String getOwnerName() { return ownerName; }
    public long getBalanceCents() { return balanceCents; }
}
