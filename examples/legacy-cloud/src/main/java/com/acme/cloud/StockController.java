package com.acme.cloud;

import io.swagger.annotations.Api;
import io.swagger.annotations.ApiOperation;
import io.swagger.annotations.ApiParam;
import java.io.IOException;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;

@RestController
@Api(tags = "stock")
public class StockController {
    private final InventoryClient inventory;
    public StockController(InventoryClient inventory) { this.inventory = inventory; }

    @ApiOperation("Stock level for a SKU")
    @GetMapping("/api/stock/{sku}")
    public int stock(@ApiParam("the SKU") @PathVariable String sku) throws IOException { return inventory.stock(sku); }

    @GetMapping("/api/public/ping")
    public String ping() { return "pong"; }
}
