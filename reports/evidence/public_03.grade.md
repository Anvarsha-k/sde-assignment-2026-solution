# Grade report — public_03_load

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| arav_cancel_2 | 0.378 | +10.6% | 0.253 | +3.5% | 0.531 | +6.0% | 45% | 392 (408) |
| arav_kyc_1 | 0.470 | +32.8% | 0.255 | +18.8% | 0.635 | +6.6% | 39% | 416 (370) |
| arav_renewal_0 | 0.446 | +10.9% | 0.274 | +3.6% | 0.581 | +7.0% | 35% | 476 (440) |
| kest_cancel_2 | 0.376 | +11.0% | 0.257 | +5.6% | 0.518 | +3.9% | 26% | 408 (411) |
| kest_kyc_1 | 0.490 | +38.7% | 0.270 | +26.3% | 0.619 | +4.2% | 25% | 451 (339) |
| kest_renewal_0 | 0.457 | +14.4% | 0.274 | +4.0% | 0.594 | +9.6% | 47% | 456 (433) |
| nort_cancel_2 | 0.381 | +12.5% | 0.256 | +5.4% | 0.528 | +5.9% | 41% | 430 (393) |
| nort_kyc_1 | 0.470 | +32.7% | 0.256 | +20.0% | 0.631 | +6.1% | 37% | 399 (323) |
| nort_renewal_0 | 0.473 | +16.9% | 0.282 | +6.9% | 0.597 | +9.0% | 50% | 440 (411) |
| tide_cancel_2 | 0.374 | +10.2% | 0.251 | +2.7% | 0.528 | +5.9% | 40% | 395 (407) |
| tide_kyc_1 | 0.508 | +43.9% | 0.273 | +26.9% | 0.635 | +7.3% | 40% | 429 (356) |
| tide_renewal_0 | 0.463 | +15.4% | 0.277 | +4.7% | 0.596 | +9.8% | 50% | 440 (438) |
| ALL | 0.440 | +20.7% | 0.265 | +10.0% | 0.584 | +7.3% | 40% | 5132 (4729) |

## Changes applied by the controller

58 applied — neutral: 14, beneficial: 43, harmful: 1

- nort_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.020 → **neutral**
- nort_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- kest_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.020 → **neutral**
- kest_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- arav_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.020 → **neutral**
- arav_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- tide_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.020 → **neutral**
- tide_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- nort_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.041 → **beneficial**
- kest_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.041 → **beneficial**
- arav_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.041 → **beneficial**
- tide_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.041 → **beneficial**
- nort_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.022 → **beneficial**
- kest_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.022 → **beneficial**
- arav_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.022 → **beneficial**
- tide_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.022 → **beneficial**
- arav_cancel_2 v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- arav_renewal_0 v5 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.061 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v5 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.061 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- tide_cancel_2 v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- nort_renewal_0 v5 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.061 didΔ=+0.000 → **beneficial**
- arav_kyc_1 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.090 didΔ=+0.110 → **beneficial**
- nort_kyc_1 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.090 didΔ=+0.113 → **beneficial**
- tide_kyc_1 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.090 didΔ=+0.114 → **beneficial**
- kest_renewal_0 v5 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.061 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.023 → **harmful**
- tide_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.357 → **beneficial**
- tide_cancel_2 v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.040 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- arav_cancel_2 v4 (controller_approved) dims=['welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.097 didΔ=+0.091 → **beneficial**
- nort_cancel_2 v4 (controller_approved) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.049 didΔ=+0.091 → **beneficial**
- nort_renewal_0 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.082 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v6 (controller_approved) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.082 didΔ=+0.048 → **beneficial**
- kest_renewal_0 v6 (controller_approved) dims=['welcome_message', 'caller_id'] engageΔ=+0.062 didΔ=+0.049 → **beneficial**
- kest_kyc_1 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.090 didΔ=+0.633 → **beneficial**
- tide_cancel_2 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.062 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.040 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.054 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v7 (controller_approved) dims=['voice'] engageΔ=+0.019 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v7 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.035 → **beneficial**
- arav_renewal_0 v7 (controller_approved) dims=['welcome_message'] engageΔ=+0.062 didΔ=+0.000 → **beneficial**
- tide_kyc_1 v4 (controller_approved) dims=['welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.034 didΔ=-0.000 → **beneficial**
- nort_kyc_1 v3 (controller_approved) dims=['welcome_message', 'caller_id', 'schedule'] engageΔ=+0.018 didΔ=+0.342 → **beneficial**
- arav_kyc_1 v3 (controller_approved) dims=['welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.034 didΔ=+0.355 → **beneficial**
- tide_cancel_2 v6 (controller_auto) dims=['caller_id', 'schedule'] engageΔ=+0.000 didΔ=-0.010 → **neutral**
- kest_renewal_0 v8 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.103 → **beneficial**
- kest_cancel_2 v5 (controller_approved) dims=['voice', 'caller_id'] engageΔ=+0.055 didΔ=+0.085 → **beneficial**
- nort_kyc_1 v4 (controller_approved) dims=['voice'] engageΔ=+0.016 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v7 (controller_approved) dims=['voice', 'caller_id'] engageΔ=+0.005 didΔ=+0.186 → **beneficial**
- nort_cancel_2 v6 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.181 → **beneficial**
- arav_cancel_2 v5 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.184 → **beneficial**
- tide_cancel_2 v7 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.027 → **beneficial**
- kest_renewal_0 v9 (controller_approved) dims=['voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- nort_renewal_0 v8 (controller_approved) dims=['voice', 'caller_id'] engageΔ=+0.005 didΔ=+0.159 → **beneficial**
- arav_renewal_0 v8 (controller_approved) dims=['voice', 'caller_id'] engageΔ=+0.020 didΔ=+0.050 → **beneficial**
- tide_kyc_1 v5 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.074 → **beneficial**
- kest_kyc_1 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.034 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 46,
  "decisions": {
    "approved": 41,
    "rejected": 5,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 5
  },
  "applied_via_approval": 40,
  "auto_applied": 18,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 1,
  "peak_requests_per_24h": {
    "nort_renewal_0": 1,
    "nort_kyc_1": 1,
    "nort_cancel_2": 1,
    "arav_renewal_0": 1,
    "arav_kyc_1": 1,
    "arav_cancel_2": 1,
    "tide_renewal_0": 1,
    "tide_kyc_1": 2,
    "tide_cancel_2": 1,
    "kest_renewal_0": 2,
    "kest_kyc_1": 2,
    "kest_cancel_2": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nort_renewal_0:welcome_message=W3": {
    "3": 0.851,
    "4": 0.852
  },
  "kest_renewal_0:welcome_message=W3": {
    "3": 0.85,
    "4": 0.852
  },
  "arav_renewal_0:welcome_message=W3": {
    "3": 0.848,
    "4": 0.847
  },
  "tide_renewal_0:welcome_message=W3": {
    "3": 0.849,
    "4": 0.849
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0