# Utility Bill Rate Formula

## How a bill is calculated

Every account is billed a fixed service charge plus a per-kilowatt-hour
rate applied to usage above a free allowance included in the service
charge. The formula is:

```
total = (service_charge + rate_per_kwh * max(0, kwh_used - free_kwh))
        * (1 + surcharge_pct)
        * (1 - exemption_pct)   [if an exemption applies]
```

The free allowance, per-kWh rate, and service charge all vary by customer
class — see the Customer Classification Guide.

## Surcharges

Industrial accounts carry a grid-capacity surcharge reflecting their peak
demand impact on distribution infrastructure. Residential and commercial
accounts carry no surcharge under the current schedule.

## Why a versioned schedule matters

Rates change periodically as generation and distribution costs shift. A
bill must be checked against whichever schedule version was in effect on
its billing period end date — using last year's rate on a bill from last
month, or vice versa, produces a bill that looks wrong even when everything
else about it is correct.
