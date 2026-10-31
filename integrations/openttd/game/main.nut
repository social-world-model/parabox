/* Parabox native GameScript bridge for OpenTTD 15.3.
 * AdminPort JSON messages are events; GSAdmin.Send sends completed results.
 * No deity-mode mutations are exposed. The caller supplies a real company ID.
 */
class ParaboxBridge extends GSController {
    history = null;
    history_order = null;

    function constructor() {
        this.history = {};
        this.history_order = [];
    }

    function Save() {
        return { history = this.history, history_order = this.history_order };
    }

    function Load(version, data) {
        if ("history" in data) this.history = data.history;
        if ("history_order" in data) this.history_order = data.history_order;
    }

    function Start() {
        while (true) {
            while (GSEventController.IsEventWaiting()) {
                local event = GSEventController.GetNextEvent();
                if (event.GetEventType() == GSEvent.ET_ADMIN_PORT) {
                    this.Handle(GSEventAdminPort.Convert(event).GetObject());
                }
            }
            this.Sleep(1);
        }
    }

    function Error(code, message) {
        return { code = code, message = message };
    }

    function NativeError() {
        local description = GSError.GetLastErrorString();
        if (description == null) description = "OpenTTD command failed";
        return this.Error(GSError.GetLastError(), description);
    }

    function TownActions() {
        return {
            advertise_small = GSTown.TOWN_ACTION_ADVERTISE_SMALL,
            advertise_medium = GSTown.TOWN_ACTION_ADVERTISE_MEDIUM,
            advertise_large = GSTown.TOWN_ACTION_ADVERTISE_LARGE,
            road_rebuild = GSTown.TOWN_ACTION_ROAD_REBUILD,
            build_statue = GSTown.TOWN_ACTION_BUILD_STATUE,
            fund_buildings = GSTown.TOWN_ACTION_FUND_BUILDINGS,
            buy_rights = GSTown.TOWN_ACTION_BUY_RIGHTS,
            bribe = GSTown.TOWN_ACTION_BRIBE
        };
    }

    function Towns(offset, limit, company_id) {
        local result = [];
        local index = 0;
        foreach (town_id, value in GSTownList()) {
            if (index >= offset && result.len() < limit) {
                result.append({ id = town_id, name = GSTown.GetName(town_id),
                    population = GSTown.GetPopulation(town_id),
                    tile = GSTown.GetLocation(town_id),
                    rating = GSTown.GetDetailedRating(town_id, company_id) });
            }
            index++;
            if (result.len() >= limit) break;
        }
        return result;
    }

    function Engines(offset, limit) {
        local result = [];
        local index = 0;
        foreach (engine_id, value in GSEngineList(GSVehicle.VT_ROAD)) {
            if (!GSEngine.IsBuildable(engine_id)) continue;
            if (index >= offset && result.len() < limit) {
                result.append({ id = engine_id, name = GSEngine.GetName(engine_id),
                    price = GSEngine.GetPrice(engine_id) });
            }
            index++;
            if (result.len() >= limit) break;
        }
        return result;
    }

    function Vehicles(offset, limit) {
        local result = [];
        local index = 0;
        foreach (vehicle_id, value in GSVehicleList()) {
            if (!GSVehicle.IsPrimaryVehicle(vehicle_id)) continue;
            if (index >= offset && result.len() < limit) {
                local orders = GSOrder.GetOrderCount(vehicle_id);
                local first_destination = null;
                if (orders > 0 && GSOrder.IsValidVehicleOrder(vehicle_id, 0)) {
                    first_destination = GSOrder.GetOrderDestination(vehicle_id, 0);
                }
                result.append({ id = vehicle_id, tile = GSVehicle.GetLocation(vehicle_id),
                    state = GSVehicle.GetState(vehicle_id),
                    profit_this_year = GSVehicle.GetProfitThisYear(vehicle_id),
                    order_count = orders, first_destination_tile = first_destination });
            }
            index++;
            if (result.len() >= limit) break;
        }
        return result;
    }

    function Stations(offset, limit) {
        local result = [];
        local index = 0;
        foreach (station_id, value in GSStationList(GSStation.STATION_ANY)) {
            if (index >= offset && result.len() < limit) {
                result.append({ id = station_id, name = GSStation.GetName(station_id),
                    tile = GSStation.GetLocation(station_id) });
            }
            index++;
            if (result.len() >= limit) break;
        }
        return result;
    }

    function Setting(name) {
        return GSGameSettings.IsValid(name) ? GSGameSettings.GetValue(name) : null;
    }

    function Snapshot(company_id) {
        local date = GSDate.GetCurrentDate();
        local settings = {};
        settings["economy.bribe"] <- this.Setting("economy.bribe");
        settings["economy.fund_roads"] <- this.Setting("economy.fund_roads");
        settings["economy.fund_buildings"] <- this.Setting("economy.fund_buildings");
        settings["economy.exclusive_rights"] <- this.Setting("economy.exclusive_rights");
        settings["economy.town_growth_rate"] <- this.Setting("economy.town_growth_rate");
        settings["difficulty.vehicle_breakdowns"] <- this.Setting("difficulty.vehicle_breakdowns");
        local vehicles = GSVehicleList();
        local stations = GSStationList(GSStation.STATION_ANY);
        return {
            tick = GSController.GetTick(),
            map_width = GSMap.GetMapSizeX(),
            map_height = GSMap.GetMapSizeY(),
            date = { year = GSDate.GetYear(date), month = GSDate.GetMonth(date),
                     day = GSDate.GetDayOfMonth(date) },
            cash = GSCompany.GetBankBalance(company_id),
            loan = GSCompany.GetLoanAmount(),
            current_quarter_income = GSCompany.GetQuarterlyIncome(company_id, GSCompany.CURRENT_QUARTER),
            current_quarter_expenses = GSCompany.GetQuarterlyExpenses(company_id, GSCompany.CURRENT_QUARTER),
            company_id = company_id,
            company_active = true,
            settings = settings,
            town_count = GSTown.GetTownCount(),
            towns = this.Towns(0, 4, company_id),
            vehicle_count = vehicles.Count(),
            station_count = stations.Count()
        };
    }

    function Execute(op, args, company_id) {
        if (op == "observe") return { ok = true, data = this.Snapshot(company_id) };
        if (op == "list_towns") {
            local offset = ("offset" in args) ? args.offset : 0;
            if (offset < 0) return { ok = false, error = this.Error("invalid_offset", "offset must be nonnegative") };
            return { ok = true, data = { offset = offset, total = GSTown.GetTownCount(),
                towns = this.Towns(offset, 4, company_id) } };
        }
        if (op == "list_road_engines") {
            local offset = ("offset" in args) ? args.offset : 0;
            if (offset < 0) return { ok = false, error = this.Error("invalid_offset", "offset must be nonnegative") };
            return { ok = true, data = { offset = offset, engines = this.Engines(offset, 4) } };
        }
        if (op == "inspect_tile") {
            if (!("tile" in args) || !GSMap.IsValidTile(args.tile)) {
                return { ok = false, error = this.Error("invalid_tile", "tile must be a valid map index") };
            }
            return { ok = true, data = { tile = args.tile,
                x = GSMap.GetTileX(args.tile), y = GSMap.GetTileY(args.tile),
                buildable = GSTile.IsBuildable(args.tile),
                owner = GSTile.GetOwner(args.tile),
                town_authority = GSTile.GetTownAuthority(args.tile),
                road = GSRoad.IsRoadTile(args.tile) } };
        }
        if (op == "inspect_town") {
            if (!("town_id" in args) || !GSTown.IsValidTown(args.town_id)) {
                return { ok = false, error = this.Error("invalid_town", "town_id must be valid") };
            }
            local available = {};
            foreach (name, action in this.TownActions()) {
                available[name] <- GSTown.IsActionAvailable(args.town_id, action);
            }
            return { ok = true, data = { town_id = args.town_id,
                name = GSTown.GetName(args.town_id),
                rating = GSTown.GetDetailedRating(args.town_id, company_id),
                available_actions = available } };
        }
        if (op == "list_vehicles") {
            local offset = ("offset" in args) ? args.offset : 0;
            if (offset < 0) return { ok = false, error = this.Error("invalid_offset", "offset must be nonnegative") };
            return { ok = true, data = { offset = offset, vehicles = this.Vehicles(offset, 4) } };
        }
        if (op == "list_stations") {
            local offset = ("offset" in args) ? args.offset : 0;
            if (offset < 0) return { ok = false, error = this.Error("invalid_offset", "offset must be nonnegative") };
            return { ok = true, data = { offset = offset, stations = this.Stations(offset, 4) } };
        }

        local native_ok = false;
        local result = {};
        if (op == "town_action") {
            local actions = this.TownActions();
            if (!("town_id" in args) || !("town_action" in args) || !actions.rawin(args.town_action)) {
                return { ok = false, error = this.Error("invalid_arguments", "town_id and known town_action required") };
            }
            local town_id = args.town_id;
            local town_action = actions[args.town_action];
            if (!GSTown.IsValidTown(town_id)) {
                return { ok = false, error = this.Error("invalid_town", "unknown town ID"),
                    data = { town_id = town_id, town_action = args.town_action, native_command_executed = false } };
            }
            local before_rating = GSTown.GetDetailedRating(town_id, company_id);
            local available_before = GSTown.IsActionAvailable(town_id, town_action);
            if (!available_before) {
                return { ok = false, error = this.Error("action_unavailable", "OpenTTD says this town action is unavailable"),
                    data = { town_id = town_id, town_action = args.town_action,
                        available_before = false, rating_before = before_rating,
                        native_command_executed = false } };
            }
            native_ok = GSTown.PerformTownAction(town_id, town_action);
            result = { town_id = town_id, town_action = args.town_action,
                available_before = available_before,
                available_after = GSTown.IsActionAvailable(town_id, town_action),
                rating_before = before_rating,
                rating_after = GSTown.GetDetailedRating(town_id, company_id),
                native_command_executed = true,
                native_return = native_ok };
            /* A detected bribe returns true from PerformTownAction. Its rating
             * and future availability show the consequence; no invented flag. */
        } else if (op == "build_road") {
            if (!("start_tile" in args) || !("end_tile" in args)) return { ok = false, error = this.Error("invalid_arguments", "start_tile and end_tile required") };
            GSRoad.SetCurrentRoadType(GSRoad.ROADTYPE_ROAD);
            native_ok = GSRoad.BuildRoad(args.start_tile, args.end_tile);
            result = { native_command_executed = true, native_return = native_ok };
        } else if (op == "build_road_depot") {
            if (!("tile" in args) || !("front_tile" in args)) return { ok = false, error = this.Error("invalid_arguments", "tile and front_tile required") };
            GSRoad.SetCurrentRoadType(GSRoad.ROADTYPE_ROAD);
            native_ok = GSRoad.BuildRoadDepot(args.tile, args.front_tile);
            result = { native_command_executed = true, native_return = native_ok };
        } else if (op == "build_road_station") {
            if (!("tile" in args) || !("front_tile" in args) || !("vehicle_type" in args)) return { ok = false, error = this.Error("invalid_arguments", "tile, front_tile, vehicle_type required") };
            if (args.vehicle_type != "bus" && args.vehicle_type != "truck") return { ok = false, error = this.Error("invalid_vehicle_type", "choose bus or truck") };
            GSRoad.SetCurrentRoadType(GSRoad.ROADTYPE_ROAD);
            local road_vehicle_type = args.vehicle_type == "bus" ? GSRoad.ROADVEHTYPE_BUS : GSRoad.ROADVEHTYPE_TRUCK;
            native_ok = GSRoad.BuildRoadStation(args.tile, args.front_tile, road_vehicle_type, GSStation.STATION_NEW);
            result = { native_command_executed = true, native_return = native_ok };
            if (native_ok) result.station_id <- GSStation.GetStationID(args.tile);
        } else if (op == "build_vehicle") {
            if (!("depot_tile" in args) || !("engine_id" in args)) return { ok = false, error = this.Error("invalid_arguments", "depot_tile and engine_id required") };
            local vehicle_id = GSVehicle.BuildVehicle(args.depot_tile, args.engine_id);
            native_ok = GSVehicle.IsValidVehicle(vehicle_id);
            result = { native_command_executed = true, native_return = native_ok,
                vehicle_id = vehicle_id };
        } else if (op == "append_order") {
            if (!("vehicle_id" in args) || !("destination_tile" in args)) return { ok = false, error = this.Error("invalid_arguments", "vehicle_id and destination_tile required") };
            native_ok = GSOrder.AppendOrder(args.vehicle_id, args.destination_tile, GSOrder.OF_NONE);
            result = { native_command_executed = true, native_return = native_ok };
        } else if (op == "start_stop_vehicle") {
            if (!("vehicle_id" in args)) return { ok = false, error = this.Error("invalid_arguments", "vehicle_id required") };
            native_ok = GSVehicle.StartStopVehicle(args.vehicle_id);
            result = { native_command_executed = true, native_return = native_ok };
        } else {
            return { ok = false, error = this.Error("unknown_operation", op) };
        }
        if (native_ok) return { ok = true, data = result };
        return { ok = false, error = this.NativeError(), data = result };
    }

    function Handle(request) {
        if (typeof request != "table" || !("protocol" in request) || request.protocol != "parabox-openttd-v1") return;
        if (!("request_id" in request) || typeof request.request_id != "string" || request.request_id.len() > 64) return;
        local id = request.request_id;
        if (this.history.rawin(id)) {
            GSAdmin.Send(this.history[id]);
            return;
        }
        local op = ("op" in request) ? request.op : "";
        local response = { protocol = "parabox-openttd-v1", request_id = id,
            ok = false, data = null, error = null,
            tick_start = GSController.GetTick(), tick_end = null };
        try {
            if (!("op" in request) || !("args" in request) || !("company_id" in request)) {
                response.error = this.Error("invalid_request", "op, args, and company_id required");
            } else {
                local mode = GSCompanyMode(request.company_id);
                if (!GSCompanyMode.IsValid()) {
                    if (op == "observe") {
                        local date = GSDate.GetCurrentDate();
                        response.ok = true;
                        response.data = { company_id = request.company_id, company_active = false,
                            tick = GSController.GetTick(), date = {
                                year = GSDate.GetYear(date), month = GSDate.GetMonth(date),
                                day = GSDate.GetDayOfMonth(date) } };
                    } else {
                        response.error = this.Error("invalid_company", "company ID does not exist");
                    }
                } else {
                    local outcome = this.Execute(request.op, request.args, request.company_id);
                    response.ok = outcome.ok;
                    if ("data" in outcome) response.data = outcome.data;
                    if ("error" in outcome) response.error = outcome.error;
                }
            }
        } catch (error) {
            response.error = this.Error("script_exception", error.tostring());
        }
        response.tick_end = GSController.GetTick();
        if (op != "observe" && op != "list_towns" && op != "list_road_engines" && op != "inspect_tile" && op != "inspect_town" && op != "list_vehicles" && op != "list_stations") {
            this.history[id] <- response;
            this.history_order.append(id);
            if (this.history_order.len() > 64) {
                local oldest = this.history_order.remove(0);
                this.history.rawdelete(oldest);
            }
        }
        GSAdmin.Send(response);
    }
}
