# Wind-down: projected liquid cash at 18:00 ET 3 Nov, by holding (24 Oct 2026, ~15:00 UTC)

Read-only.
* Positions: the fills ledger netted per leg. It matches the venue to the share (RI Governor 12,304 NO sets, MN-05 21,162 YES sets, Hawaii Governor 7,658 YES sets).
* Depth: `data/books_latest.json`. A leg missing there is taken from the newest `data/books_history` snapshot.
* Favourite: the latest Polymarket/Kalshi fair, else the SIG mid.
* Cash: the last cycle.
* Script: `research_notes/wind_down_projection.py` (in the wiring diff). It runs the same `WD.project` the bot writes hourly to `params/wind_down_report.json` once the routine is deployed.

**Sale value** is the walked proceeds of one clip (at most 1,000 sets; the walk is shrunk to at most 1 tick a set) applied to the whole holding, assuming the book refills between 30-min clips.

**Call class (backlog 000a).**
* Late:
  * a state whose last polls close after 21:00 ET;
  * a slow-count state;
  * Georgia (runoff);
  * a chamber market;
  * a favourite below 0.85;
  * Rhode Island Governor (forced, decision 00a).
* Early: every other race. Early-call sets are held through the call (selling at the call is report-only until Sizhe approves it).

**Headline.**
* Cash now: about 1.0k.
* By 18:00 ET 3 Nov: about **59k**. That is the late-call sets (33.1k) plus the single legs (25.2k), at today's prices and exit costs.
* If the early-call sets are also sold at the call: about **81k**. Those sets are worth 21.9k at today's touch and 22.5k at payout. MN-05's 21,162 YES sets (20.5k) are most of it: Minnesota closes at 21:00 ET and the favourite is at 0.95, so they are early-call.
* Selling the early-call sets before 18:00 instead costs about 0.65k against payout. Sizhe's punt time (asked in the 1 Nov brief) decides between the two.
* The exempt sleeves' holdings are not in the totals:
  * cheap favourites (West Virginia Senate, Wyoming Senate);
  * high-frequency arb lots (Tennessee Governor, MT-01: about 4k of sets, sold by 17:30 ET 3 Nov by that sleeve).
* The backlog's ~95-100k assumed everything at payout. The gap is mainly exit costs (about 1.9k on sets) and legs valued at the bid rather than at the mark.

| holding | sets | call | why | payout | exit cost now (ticks, touch) | clip | sale per set | sale value | cost vs payout | min hours | when |
|---|---|---|---|---|---|---|---|---|---|---|---|
| MN-05 House race (YES) | 21,162 | early | MN polls close by 21:00 ET, favourite 0.95 | 21,162 | 5.0 | 1000 | 0.9703 | 20,534 | 628 | 11.0 | held through the call (selling at the call is report-only) |
| New Hampshire Senate (YES) | 1,084 | early | NH polls close by 20:00 ET, favourite 0.90 | 1,084 | 4.0 | 1000 | 0.98 | 1,062 | 22 | 1.0 | held through the call (selling at the call is report-only) |
| PA-07 House race (NO) | 265 | early | PA polls close by 20:00 ET, favourite 0.87 | 265 | 3.0 | 265 | 0.985 | 261 | 4 | 0.5 | held through the call (selling at the call is report-only) |
| Rhode Island Governor (NO) | 12,304 | late | sold anyway (force_late) | 24,608 | 14.0 | 1000 | 1.9279 | 23,721 | 887 | 6.5 | 31 Oct-3 Nov 18:00 ET, clips every 30 min |
| Hawaii Governor (YES) | 7,658 | late | HI polls close 24:00 ET | 7,658 | 9.0 | 1000 | 0.955 | 7,313 | 345 | 4.0 | 31 Oct-3 Nov 18:00 ET, clips every 30 min |
| Iowa Senate (NO) | 470 | late | close race (favourite 0.58) | 470 | 5.0 | 470 | 0.975 | 458 | 12 | 0.5 | 31 Oct-3 Nov 18:00 ET, clips every 30 min |
| Georgia Governor (NO) | 420 | late | GA runoff rules | 420 | 3.0 | 420 | 0.985 | 414 | 6 | 0.5 | 31 Oct-3 Nov 18:00 ET, clips every 30 min |
| CO-04 House race (NO) | 379 | late | close race (favourite 0.71) | 379 | 4.0 | 379 | 0.98 | 371 | 8 | 0.5 | 31 Oct-3 Nov 18:00 ET, clips every 30 min |
| FL-16 House race (NO) | 317 | late | close race (favourite 0.80) | 317 | 2.0 | 317 | 0.99 | 314 | 3 | 0.5 | 31 Oct-3 Nov 18:00 ET, clips every 30 min |
| PA-01 House race (NO) | 287 | late | close race (favourite 0.60) | 287 | 3.0 | 287 | 0.985 | 283 | 4 | 0.5 | 31 Oct-3 Nov 18:00 ET, clips every 30 min |
| IA-03 House race (NO) | 163 | late | close race (favourite 0.85) | 163 | 3.0 | 163 | 0.985 | 161 | 2 | 0.5 | 31 Oct-3 Nov 18:00 ET, clips every 30 min |
| WI-03 House race (YES) | 30 | late | close race (favourite 0.77) | 30 | -3.0 | 30 | 1.015 | 30 | -0 | 0.5 | 31 Oct-3 Nov 18:00 ET, clips every 30 min |
| MI-10 House race (NO) | 18 | late | close race (favourite 0.72) | 18 | 1.0 | 18 | 0.995 | 18 | 0 | 0.5 | 31 Oct-3 Nov 18:00 ET, clips every 30 min |
| WI-01 House race (NO) | 7 | late | close race (favourite 0.55) | 7 | 4.0 | 7 | 0.98 | 7 | 0 | 0.5 | 31 Oct-3 Nov 18:00 ET, clips every 30 min |

| leg | shares | side | sale per share (touch) | sale value | when |
|---|---|---|---|---|---|
| Idaho Governor leg 859 | 2,339 | no | 0.925 | 2,164 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| California Governor leg 851 | 1,735 | no | 0.905 | 1,570 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| CA-48 House race leg 893 | 1,191 | no | 0.875 | 1,042 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Montana Senate leg 963 | 1,080 | no | 0.955 | 1,031 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Vermont Governor leg 1001 | 1,426 | no | 0.715 | 1,020 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Hawaii Governor leg 990 | 1,085 | yes | 0.915 | 993 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| New Hampshire Senate leg 1070 | 1,096 | yes | 0.84 | 921 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Florida Governor leg 988 | 971 | no | 0.775 | 752 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| North Carolina Senate leg 966 | 779 | yes | 0.94 | 732 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| New Jersey Senate leg 972 | 766 | no | 0.945 | 724 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Florida Senate leg 1043 | 779 | no | 0.86 | 670 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Idaho Senate leg 951 | 675 | no | 0.965 | 651 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Kansas Governor leg 992 | 903 | no | 0.72 | 650 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Arkansas Governor leg 846 | 701 | no | 0.92 | 645 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Rhode Island Governor leg 880 | 684 | no | 0.94 | 643 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| WA-03 House race leg 1036 | 714 | yes | 0.85 | 607 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| KY-04 House race leg 908 | 598 | no | 0.94 | 562 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| VA-05 House race leg 1033 | 878 | no | 0.63 | 553 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Georgia Senate leg 947 | 565 | yes | 0.91 | 514 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Massachusetts Governor leg 1059 | 500 | yes | 0.92 | 460 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| NH-01 House race leg 1065 | 500 | no | 0.905 | 452 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| New Mexico Governor leg 869 | 427 | yes | 0.925 | 395 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Maine Senate leg 959 | 1,099 | no | 0.34 | 374 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Arkansas Governor leg 847 | 393 | yes | 0.93 | 366 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| FL-20 House race leg 1016 | 363 | no | 0.94 | 341 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Kansas Senate leg 1045 | 577 | no | 0.575 | 332 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Mississippi Senate leg 961 | 387 | no | 0.82 | 317 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Michigan Senate leg 1048 | 438 | no | 0.675 | 296 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Maine Governor leg 866 | 318 | no | 0.925 | 294 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| MN-02 House race leg 1026 | 317 | no | 0.91 | 288 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| NH-01 House race leg 1064 | 304 | yes | 0.925 | 281 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| AZ-01 House race leg 886 | 339 | yes | 0.82 | 278 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Massachusetts Governor leg 1060 | 292 | no | 0.94 | 274 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| New Mexico Governor leg 870 | 296 | no | 0.91 | 269 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Texas Senate leg 982 | 686 | no | 0.37 | 254 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| New York Governor leg 874 | 240 | no | 0.925 | 222 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| South Carolina Governor leg 881 | 241 | no | 0.92 | 222 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Oregon Governor leg 877 | 250 | no | 0.79 | 198 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| MI-07 House race leg 1020 | 263 | no | 0.71 | 187 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| ME-02 House race leg 910 | 348 | no | 0.53 | 184 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Mississippi Senate leg 962 | 223 | yes | 0.825 | 184 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| U.S. House leg 841 | 202 | no | 0.875 | 177 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Rhode Island Governor leg 1074 | 172 | no | 0.905 | 156 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| AL-02 House race leg 1007 | 206 | no | 0.7 | 144 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| CA-22 House race leg 891 | 147 | no | 0.88 | 129 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| TX-23 House race leg 934 | 213 | no | 0.59 | 126 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Nebraska Governor leg 867 | 133 | no | 0.85 | 113 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| MI-10 House race leg 1022 | 162 | no | 0.68 | 110 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| FL-20 House race leg 1015 | 112 | yes | 0.95 | 106 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| U.S. Senate leg 843 | 149 | no | 0.665 | 99 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Maine Senate leg 960 | 279 | yes | 0.355 | 99 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Iowa Governor leg 857 | 115 | yes | 0.85 | 98 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| CO-08 House race leg 898 | 100 | yes | 0.83 | 83 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Nebraska Senate leg 969 | 101 | no | 0.71 | 72 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| North Carolina Senate leg 967 | 77 | no | 0.925 | 71 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| WI-03 House race leg 1041 | 282 | yes | 0.25 | 70 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| TX-15 House race leg 932 | 79 | yes | 0.855 | 68 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Texas Governor leg 884 | 100 | no | 0.67 | 67 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| FL-13 House race leg 901 | 229 | no | 0.275 | 63 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| VA-01 House race leg 938 | 100 | no | 0.615 | 62 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Iowa Senate leg 949 | 108 | no | 0.545 | 59 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Alaska Governor leg 1058 | 53 | no | 0.855 | 45 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| NY-17 House race leg 920 | 72 | yes | 0.62 | 45 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| Georgia Governor leg 855 | 105 | no | 0.39 | 41 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| U.S. Senate leg 842 | 58 | yes | 0.67 | 39 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| ME-02 House race leg 911 | 60 | yes | 0.545 | 33 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| California Governor leg 850 | 33 | yes | 0.92 | 30 | 31 Oct-3 Nov, within 1-3 ticks of fair |
| 11 smaller legs | | | | 124 | |

| exempt holding | sleeve | legs | when |
|---|---|---|---|
| Alabama Senate | cheap favourites | {'942': 518, '941': -963} | cheap favourites: own schedule |
| Colorado Senate | cheap favourites | {'946': -485, '945': 1493} | cheap favourites: own schedule |
| Connecticut Governor | cheap favourites | {'987': 252, '854': -1} | cheap favourites: own schedule |
| Delaware Senate | cheap favourites | {'1075': -1743, '1042': 44} | cheap favourites: own schedule |
| MT-01 House race | high-frequency arb | {'913': -2000, '912': -2668} | high-frequency arb: own schedule |
| Maryland Governor | cheap favourites | {'863': 426} | cheap favourites: own schedule |
| Massachusetts Senate | cheap favourites | {'1069': -897, '1068': 1045} | cheap favourites: own schedule |
| Oklahoma Senate | cheap favourites | {'1072': -1267, '975': 529} | cheap favourites: own schedule |
| Rhode Island Senate | cheap favourites | {'1077': -349, '1076': 2574} | cheap favourites: own schedule |
| Tennessee Governor | high-frequency arb | {'1000': -2050, '999': -2745} | high-frequency arb: own schedule |
| Tennessee Senate | cheap favourites | {'1051': -1171} | cheap favourites: own schedule |
| Virginia Senate | cheap favourites | {'1053': -256, '984': 1020} | cheap favourites: own schedule |
| West Virginia Senate | cheap favourites | {'986': 589} | cheap favourites: own schedule |
| Wyoming Senate | cheap favourites | {'1055': 715, '1054': -578} | cheap favourites: own schedule |

| total | S |
|---|---|
| cash now | 1,000 |
| late sets sale | 33,090 |
| legs sale | 25,241 |
| projected cash 1800 et | 59,332 |
| early sets sale today | 21,857 |
| early sets payout | 22,511 |
| projected cash if early sold too | 81,189 |