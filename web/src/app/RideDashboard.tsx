"use client";

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

type User = {
  id: string;
  email: string;
  full_name: string;
  role: "rider" | "driver" | "admin";
};

type Coordinate = { latitude: number; longitude: number };
type Ride = {
  id: string;
  rider_id: string;
  driver_id: string | null;
  driver_name: string | null;
  driver_vehicle: string | null;
  status: string;
  pickup: Coordinate;
  dropoff: Coordinate;
  distance_meters: number;
  fare_paise: number;
  currency: "INR";
  fare_base_paise: number;
  fare_per_km_paise: number;
  request_deadline: string;
  created_at: string;
  updated_at: string;
};
type FareEstimate = Pick<
  Ride,
  "distance_meters" | "fare_paise" | "currency" | "fare_base_paise" | "fare_per_km_paise"
>;
type RideOffer = {
  attempt_id: string;
  attempt_number: number;
  expires_at: string;
  expires_in_seconds: number;
  received_at: number;
  ride: Ride;
};
type Payment = {
  id: string;
  ride_id: string;
  user_id: string;
  amount_paise: number;
  currency: "INR";
  status: "PENDING" | "PROCESSING" | "SUCCEEDED" | "FAILED";
  reference_id: string;
  attempt_count: number;
  last_error: string | null;
  created_at: string;
  updated_at: string;
  succeeded_at: string | null;
};
type DriverSummary = {
  completed_rides: number;
  gross_fare_paise: number;
  currency: "INR";
};
type AdminDashboardData = {
  users_by_role: Record<string, number>;
  drivers_total: number;
  rides_by_status: Record<string, number>;
  payments_by_status: Record<string, number>;
  completed_fare_paise: number;
  currency: "INR";
};
type AdminUser = {
  id: string;
  email: string;
  full_name: string;
  role: string;
  is_active: boolean;
  created_at: string;
};
type AdminDriver = {
  id: string;
  email: string;
  full_name: string;
  is_active: boolean;
  is_online: boolean;
  availability: string;
  verification_status: string;
};

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const activeStatuses = new Set([
  "ACCEPTED",
  "DRIVER_ARRIVING",
  "DRIVER_ARRIVED",
  "IN_PROGRESS",
]);
const cancelStatuses = new Set([
  "REQUESTED",
  "ACCEPTED",
  "DRIVER_ARRIVING",
  "DRIVER_ARRIVED",
]);
const rideUpdateStatuses = new Set(["REQUESTED", ...activeStatuses]);

function mergeRideList(current: Ride[], incoming: Ride[]): Ride[] {
  const merged = new Map(current.map((ride) => [ride.id, ride]));
  for (const ride of incoming) {
    const existing = merged.get(ride.id);
    if (!existing || Date.parse(ride.updated_at) >= Date.parse(existing.updated_at)) {
      merged.set(ride.id, ride);
    }
  }
  return [...merged.values()].sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at));
}

function remainingOfferSeconds(offer: RideOffer, now: number): number {
  const elapsed = now > offer.received_at ? Math.floor((now - offer.received_at) / 1000) : 0;
  return Math.max(0, offer.expires_in_seconds - elapsed);
}

function formatCountdown(seconds: number): string {
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

async function responseError(response: Response): Promise<string> {
  const body = (await response.json().catch(() => null)) as
    | { detail?: string | Array<{ msg?: string }> }
    | null;
  if (typeof body?.detail === "string") return body.detail;
  if (Array.isArray(body?.detail)) return body.detail[0]?.msg ?? "Please check the submitted details.";
  return "The request could not be completed.";
}

function dollars(paise: number): string {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  }).format(paise / 100);
}

function statusLabel(status: string): string {
  return status.replaceAll("_", " ").toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export default function RideDashboard({
  user,
  accessToken,
  onAccessToken,
  onLogout,
}: {
  user: User;
  accessToken: string;
  onAccessToken: (token: string) => void;
  onLogout: () => void;
}) {
  const [rides, setRides] = useState<Ride[]>([]);
  const [offers, setOffers] = useState<RideOffer[]>([]);
  const [payments, setPayments] = useState<Payment[]>([]);
  const [driverSummary, setDriverSummary] = useState<DriverSummary | null>(null);
  const [adminSummary, setAdminSummary] = useState<AdminDashboardData | null>(null);
  const [adminUsers, setAdminUsers] = useState<AdminUser[]>([]);
  const [adminDrivers, setAdminDrivers] = useState<AdminDriver[]>([]);
  const [adminRides, setAdminRides] = useState<Ride[]>([]);
  const [adminReason, setAdminReason] = useState("");
  const [quote, setQuote] = useState<FareEstimate | null>(null);
  const [pickupLatitude, setPickupLatitude] = useState("");
  const [pickupLongitude, setPickupLongitude] = useState("");
  const [dropoffLatitude, setDropoffLatitude] = useState("");
  const [dropoffLongitude, setDropoffLongitude] = useState("");
  const [driverLatitude, setDriverLatitude] = useState("");
  const [driverLongitude, setDriverLongitude] = useState("");
  const [online, setOnline] = useState(false);
  const [offerClockNow, setOfferClockNow] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const expiredOfferRefreshes = useRef(new Set<string>());

  const request = useCallback(
    async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
      const send = (token: string) =>
        fetch(`${apiBase}${path}`, {
          ...init,
          headers: {
            ...(init.body ? { "Content-Type": "application/json" } : {}),
            Authorization: `Bearer ${token}`,
            ...init.headers,
          },
        });
      let response = await send(accessToken);
      if (response.status === 401) {
        const refreshed = await fetch(`${apiBase}/api/v1/auth/refresh`, {
          method: "POST",
          credentials: "include",
        });
        if (!refreshed.ok) {
          onLogout();
          throw new Error("Your session has expired. Please sign in again.");
        }
        const session = (await refreshed.json()) as { access_token: string };
        onAccessToken(session.access_token);
        response = await send(session.access_token);
      }
      if (!response.ok) throw new Error(await responseError(response));
      if (response.status === 204) return undefined as T;
      return (await response.json()) as T;
    },
    [accessToken, onAccessToken, onLogout],
  );

  const refreshData = useCallback(async () => {
    const [
      nextRides,
      nextOffers,
      nextAvailability,
      nextDriverSummary,
      nextPayments,
      nextAdminSummary,
      nextAdminUsers,
      nextAdminDrivers,
      nextAdminRides,
      nextAdminPayments,
    ] = await Promise.all([
      request<Ride[]>("/api/v1/rides"),
      user.role === "driver" ? request<RideOffer[]>("/api/v1/drivers/me/offers") : Promise.resolve([]),
      user.role === "driver"
        ? request<{ online: boolean }>("/api/v1/drivers/me/availability")
        : Promise.resolve(null),
      user.role === "driver"
        ? request<DriverSummary>("/api/v1/drivers/me/summary")
        : Promise.resolve(null),
      user.role === "rider" ? request<Payment[]>("/api/v1/payments") : Promise.resolve([]),
      user.role === "admin"
        ? request<AdminDashboardData>("/api/v1/admin/dashboard")
        : Promise.resolve(null),
      user.role === "admin" ? request<AdminUser[]>("/api/v1/admin/users") : Promise.resolve([]),
      user.role === "admin" ? request<AdminDriver[]>("/api/v1/admin/drivers") : Promise.resolve([]),
      user.role === "admin" ? request<Ride[]>("/api/v1/admin/rides") : Promise.resolve([]),
      user.role === "admin" ? request<Payment[]>("/api/v1/admin/payments") : Promise.resolve([]),
    ]);
    setRides((current) => mergeRideList(current, user.role === "admin" ? nextAdminRides : nextRides));
    setOffers(
      nextOffers.map((offer) => ({
        ...offer,
        received_at: performance.now(),
      })),
    );
    const currentOfferIds = new Set(nextOffers.map((offer) => offer.attempt_id));
    for (const attemptId of expiredOfferRefreshes.current) {
      if (!currentOfferIds.has(attemptId)) expiredOfferRefreshes.current.delete(attemptId);
    }
    if (nextAvailability) setOnline(nextAvailability.online);
    setDriverSummary(nextDriverSummary);
    setPayments(user.role === "admin" ? nextAdminPayments : nextPayments);
    setAdminSummary(nextAdminSummary);
    setAdminUsers(nextAdminUsers);
    setAdminDrivers(nextAdminDrivers);
    setAdminRides(nextAdminRides);
  }, [request, user.role]);

  useEffect(() => {
    let active = true;
    const initialLoad = setTimeout(() => {
      refreshData()
        .catch((reason: unknown) => {
          if (active) setError(reason instanceof Error ? reason.message : "Unable to load your rides.");
        })
        .finally(() => {
          if (active) setLoading(false);
        });
    }, 0);
    return () => {
      active = false;
      clearTimeout(initialLoad);
    };
  }, [refreshData]);

  const rideSubscriptionKey = useMemo(
    () =>
      JSON.stringify(
        rides
          .filter((ride) =>
            user.role === "rider"
              ? rideUpdateStatuses.has(ride.status)
              : user.role === "driver" &&
                ride.driver_id === user.id &&
                activeStatuses.has(ride.status),
          )
          .map((ride) => ride.id),
      ),
    [rides, user.id, user.role],
  );

  useEffect(() => {
    const rideIds = JSON.parse(rideSubscriptionKey) as string[];
    const targets = [
      ...rideIds.map((rideId) => ({
        key: `ride:${rideId}`,
        rideId,
        path: `/api/v1/ws/rides/${rideId}`,
      })),
      ...(user.role === "driver"
        ? [{ key: "offers", rideId: null, path: "/api/v1/ws/drivers/offers" }]
        : []),
    ];
    if (targets.length === 0) return;
    const sockets = new Map<string, WebSocket>();
    const retryTimers = new Map<string, ReturnType<typeof setTimeout>>();
    const heartbeats = new Map<string, ReturnType<typeof setInterval>>();
    let stopped = false;
    const connect = async (target: (typeof targets)[number]) => {
      if (stopped) return;
      try {
        const { ticket } = await request<{ ticket: string; expires_in: number }>("/api/v1/ws/tickets", {
          method: "POST",
        });
        if (stopped) return;
        const socketUrl = new URL(target.path, apiBase);
        socketUrl.protocol = socketUrl.protocol === "https:" ? "wss:" : "ws:";
        socketUrl.searchParams.set("ticket", ticket);
        const socket = new WebSocket(socketUrl);
        sockets.set(target.key, socket);
      } catch (reason) {
        if (!stopped) {
          setError(reason instanceof Error ? reason.message : "Unable to connect to ride updates.");
          retryTimers.set(target.key, setTimeout(() => void connect(target), 3_000));
        }
        return;
      }
      const socket = sockets.get(target.key);
      if (!socket) return;
      socket.onopen = () => {
        const heartbeat = setInterval(() => {
          if (socket?.readyState === WebSocket.OPEN) socket.send("ping");
        }, 20_000);
        heartbeats.set(target.key, heartbeat);
      };
      socket.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data) as { type?: string; ride?: Ride };
          const updatedRide = message.ride;
          if (message.type === "pong") return;
          if (
            target.rideId &&
            message.type === "ride_state" &&
            updatedRide?.id === target.rideId
          ) {
            setRides((current) => mergeRideList(current, [updatedRide]));
          } else if (target.key === "offers") {
            void refreshData().catch((reason: unknown) =>
              setError(reason instanceof Error ? reason.message : "Unable to refresh ride updates."),
            );
          }
        } catch {
          setError("Received an unreadable live update.");
        }
      };
      socket.onclose = () => {
        const heartbeat = heartbeats.get(target.key);
        if (heartbeat) clearInterval(heartbeat);
        heartbeats.delete(target.key);
        if (!stopped) retryTimers.set(target.key, setTimeout(() => void connect(target), 3_000));
      };
    };
    targets.forEach((target) => void connect(target));
    return () => {
      stopped = true;
      for (const timer of retryTimers.values()) clearTimeout(timer);
      for (const heartbeat of heartbeats.values()) clearInterval(heartbeat);
      for (const socket of sockets.values()) socket.close();
    };
  }, [accessToken, refreshData, request, rideSubscriptionKey, user.role]);

  useEffect(() => {
    if (user.role !== "driver" || offers.length === 0) return;
    const timer = window.setInterval(() => setOfferClockNow(performance.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [offers, user.role]);

  useEffect(() => {
    if (user.role !== "driver") return;
    const expired = offers.filter(
      (offer) =>
        remainingOfferSeconds(offer, offerClockNow) === 0 &&
        !expiredOfferRefreshes.current.has(offer.attempt_id),
    );
    if (expired.length === 0) return;
    for (const offer of expired) expiredOfferRefreshes.current.add(offer.attempt_id);
    void refreshData().catch((reason: unknown) =>
      setError(reason instanceof Error ? reason.message : "Unable to check expired offers."),
    );
  }, [offerClockNow, offers, refreshData, user.role]);

  async function perform(action: () => Promise<void>, success?: string) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
      if (success) setNotice(success);
      await refreshData();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The action could not be completed.");
    } finally {
      setBusy(false);
    }
  }

  async function estimateFare(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setQuote(null);
    await perform(async () => {
      const estimate = await request<FareEstimate>("/api/v1/rides/estimate", {
        method: "POST",
        body: JSON.stringify({
          pickup: { latitude: Number(pickupLatitude), longitude: Number(pickupLongitude) },
          dropoff: { latitude: Number(dropoffLatitude), longitude: Number(dropoffLongitude) },
        }),
      });
      setQuote(estimate);
    }, "Fare estimate is ready.");
  }

  async function requestRide() {
    if (!quote) return;
    await perform(async () => {
      const ride = await request<Ride>("/api/v1/rides", {
        method: "POST",
        body: JSON.stringify({
          pickup: { latitude: Number(pickupLatitude), longitude: Number(pickupLongitude) },
          dropoff: { latitude: Number(dropoffLatitude), longitude: Number(dropoffLongitude) },
        }),
      });
      setRides((current) => mergeRideList(current, [ride]));
      setQuote(null);
    }, "Ride request submitted.");
  }

  async function cancelRide(ride: Ride) {
    await perform(async () => {
      await request<Ride>(`/api/v1/rides/${ride.id}/cancel`, {
        method: "POST",
        body: JSON.stringify({ reason: "Cancelled by rider" }),
      });
    }, "Ride cancelled.");
  }

  async function sendDriverLocation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await perform(async () => {
      await request("/api/v1/drivers/me/location", {
        method: "POST",
        body: JSON.stringify({
          latitude: Number(driverLatitude),
          longitude: Number(driverLongitude),
        }),
      });
      setNotice("Location updated.");
    });
  }

  async function setDriverOnline(nextOnline: boolean) {
    await perform(async () => {
      const result = await request<{ online: boolean }>("/api/v1/drivers/me/availability", {
        method: "POST",
        body: JSON.stringify({ online: nextOnline }),
      });
      setOnline(result.online);
    }, nextOnline ? "You are online and available for offers." : "You are offline.");
  }

  async function respondToOffer(offer: RideOffer, choice: "accept" | "reject") {
    await perform(async () => {
      await request<Ride>(`/api/v1/rides/${offer.ride.id}/offers/${choice}`, { method: "POST" });
    }, choice === "accept" ? "Ride accepted." : "Offer declined.");
  }

  async function advanceRide(ride: Ride, action: "arriving" | "arrived" | "start" | "complete") {
    await perform(async () => {
      await request<Ride>(`/api/v1/rides/${ride.id}/${action}`, {
        method: "POST",
        body: JSON.stringify({ reason: null }),
      });
    }, `Ride updated: ${statusLabel(action === "start" ? "IN_PROGRESS" : action === "complete" ? "COMPLETED" : action === "arriving" ? "DRIVER_ARRIVING" : "DRIVER_ARRIVED")}.`);
  }

  async function processPayment(payment: Payment) {
    await perform(async () => {
      const updated = await request<Payment>(`/api/v1/payments/${payment.id}/process`, {
        method: "POST",
      });
      setPayments((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      if (updated.status === "FAILED") {
        throw new Error(`Local mock payment failed: ${statusLabel(updated.last_error ?? "payment_failed")}.`);
      }
    }, "Local mock checkout completed.");
  }

  async function changeDriverVerification(driver: AdminDriver, verified: boolean) {
    if (!adminReason.trim()) {
      setError("Enter an audit reason before changing driver verification.");
      return;
    }
    await perform(async () => {
      await request(`/api/v1/admin/drivers/${driver.id}/verification`, {
        method: "POST",
        body: JSON.stringify({ verified, reason: adminReason.trim() }),
      });
      setAdminReason("");
    }, verified ? "Driver verified." : "Driver rejected.");
  }

  async function changeUserActivation(target: AdminUser, isActive: boolean) {
    if (!adminReason.trim()) {
      setError("Enter an audit reason before changing account access.");
      return;
    }
    await perform(async () => {
      await request(`/api/v1/admin/users/${target.id}/activation`, {
        method: "POST",
        body: JSON.stringify({ is_active: isActive, reason: adminReason.trim() }),
      });
      setAdminReason("");
    }, isActive ? "User account activated." : "User account deactivated and sessions revoked.");
  }

  async function cancelAdminRide(ride: Ride) {
    if (!adminReason.trim()) {
      setError("Enter an audit reason before cancelling a ride.");
      return;
    }
    const releaseConfirmed =
      ride.status !== "IN_PROGRESS" ||
      window.confirm("This trip is in progress. Confirm that the assigned driver should be released?");
    if (!releaseConfirmed) return;
    await perform(async () => {
      await request(`/api/v1/rides/${ride.id}/admin-cancel`, {
        method: "POST",
        body: JSON.stringify({
          reason: adminReason.trim(),
          confirm_driver_release: releaseConfirmed,
        }),
      });
      setAdminReason("");
    }, "Ride cancelled and administrative action audited.");
  }

  const assignedRides = rides.filter((ride) => ride.driver_id === user.id);

  return (
    <div className="dashboard">
      <div className="dashboard-heading">
        <div>
          <p className="eyebrow">{user.role.toUpperCase()} DASHBOARD</p>
          <h2>Welcome, {user.full_name.split(" ")[0]}.</h2>
          <p className="dashboard-subtitle">
            {user.role === "rider"
              ? "Plan a trip, follow its progress, and find your past rides."
              : user.role === "driver"
                ? "Manage availability and offers, review completed trips, and track gross completed fares."
                : "Review platform activity, verify drivers, and inspect rides and simulated payments."}
          </p>
        </div>
        <button className="button button-secondary logout-button" onClick={onLogout} type="button">
          Sign out
        </button>
      </div>

      {error && <p className="dashboard-alert error-alert" role="alert">{error}</p>}
      {notice && <p className="dashboard-alert success-alert" role="status">{notice}</p>}

      {user.role === "rider" && (
        <div className="dashboard-grid">
          <section className="dashboard-panel">
            <h3>Request a ride</h3>
            <p className="panel-hint">Enter decimal latitude and longitude for both points.</p>
            <form className="ride-form" onSubmit={estimateFare}>
              <CoordinateFields
                id="pickup"
                label="Pickup"
                latitude={pickupLatitude}
                longitude={pickupLongitude}
                setLatitude={setPickupLatitude}
                setLongitude={setPickupLongitude}
              />
              <CoordinateFields
                id="dropoff"
                label="Drop-off"
                latitude={dropoffLatitude}
                longitude={dropoffLongitude}
                setLatitude={setDropoffLatitude}
                setLongitude={setDropoffLongitude}
              />
              <button className="button button-primary" disabled={busy} type="submit">
                {busy ? "Working…" : "Calculate fare"}
              </button>
            </form>
            {quote && (
              <div className="quote-card" role="status">
                <strong>{dollars(quote.fare_paise)}</strong>
                <span>{(quote.distance_meters / 1000).toFixed(2)} km straight-line estimate</span>
                <small>
                  Base {dollars(quote.fare_base_paise)} + {dollars(quote.fare_per_km_paise)}/km.
                </small>
                <button
                  className="button button-secondary"
                  disabled={busy}
                  onClick={() => void requestRide()}
                  type="button"
                >
                  Request this ride
                </button>
              </div>
            )}
          </section>
          <RideList
            rides={rides.filter((ride) => ride.rider_id === user.id)}
            loading={loading}
            canCancel
            busy={busy}
            payments={payments}
            onPayment={(payment) => void processPayment(payment)}
            onCancel={(ride) => void cancelRide(ride)}
            onRefresh={() => void perform(refreshData)}
          />
          <PaymentHistory payments={payments} loading={loading} />
        </div>
      )}

      {user.role === "driver" && (
        <div className="dashboard-grid">
          <section className="dashboard-panel">
            <h3>Driver availability</h3>
            <p className="panel-hint">
              Your account must be verified and your location must be fresh to go online.
            </p>
            <form className="ride-form" onSubmit={sendDriverLocation}>
              <CoordinateFields
                id="driver-location"
                label="Current location"
                latitude={driverLatitude}
                longitude={driverLongitude}
                setLatitude={setDriverLatitude}
                setLongitude={setDriverLongitude}
              />
              <button className="button button-secondary" disabled={busy} type="submit">
                Update location
              </button>
            </form>
            <button
              className={online ? "button button-secondary" : "button button-primary"}
              disabled={busy}
              onClick={() => void setDriverOnline(!online)}
              type="button"
            >
              {online ? "Go offline" : "Go online"}
            </button>
            <p className="availability-state" aria-live="polite">
              Current browser state: {online ? "online" : "offline"}.
            </p>
            {driverSummary && (
              <div className="summary-metrics">
                <div><span>Completed rides</span><strong>{driverSummary.completed_rides}</strong></div>
                <div><span>Gross completed fares</span><strong>{dollars(driverSummary.gross_fare_paise)}</strong></div>
                <p className="panel-hint">Fare totals are not payouts or settled earnings.</p>
              </div>
            )}
          </section>
          <section className="dashboard-panel">
            <div className="panel-title-row">
              <div>
                <h3>Ride offers</h3>
                <p className="panel-hint">Offers are sent to one driver at a time and expire.</p>
              </div>
              <button className="text-button" disabled={busy} onClick={() => void perform(refreshData)} type="button">
                Refresh
              </button>
            </div>
            {loading ? (
              <p className="empty-state">Loading offers…</p>
            ) : offers.length === 0 ? (
              <p className="empty-state">No current offers.</p>
            ) : (
              offers.map((offer) => (
                <article className="ride-card offer-card" key={offer.attempt_id}>
                  <RideSummary ride={offer.ride} />
                  {offerClockNow > 0 && remainingOfferSeconds(offer, offerClockNow) === 0 ? (
                    <p className="offer-expiry offer-expired" role="status">
                      Offer expired · checking with server…
                    </p>
                  ) : (
                    <p className="offer-expiry" aria-live="off">
                      Offer {offer.attempt_number} · expires in{" "}
                      {formatCountdown(remainingOfferSeconds(offer, offerClockNow))}
                    </p>
                  )}
                  <div className="ride-actions">
                    <button className="button button-primary" disabled={busy || remainingOfferSeconds(offer, offerClockNow) === 0} onClick={() => void respondToOffer(offer, "accept")} type="button">
                      Accept
                    </button>
                    <button className="button button-secondary" disabled={busy || remainingOfferSeconds(offer, offerClockNow) === 0} onClick={() => void respondToOffer(offer, "reject")} type="button">
                      Decline
                    </button>
                  </div>
                </article>
              ))
            )}
          </section>
          <RideList
            rides={assignedRides}
            loading={loading}
            canCancel={false}
            busy={busy}
            onCancel={() => undefined}
            onRefresh={() => void perform(refreshData)}
            onAdvance={advanceRide}
          />
        </div>
      )}

      {user.role === "admin" && (
        <AdminDashboard
          loading={loading}
          busy={busy}
          summary={adminSummary}
          users={adminUsers}
          drivers={adminDrivers}
          rides={adminRides}
          payments={payments}
          reason={adminReason}
          setReason={setAdminReason}
          currentUserId={user.id}
          onVerify={changeDriverVerification}
          onActivation={changeUserActivation}
          onCancelRide={cancelAdminRide}
          onRefresh={() => void perform(refreshData)}
        />
      )}
    </div>
  );
}

function CoordinateFields({
  id,
  label,
  latitude,
  longitude,
  setLatitude,
  setLongitude,
}: {
  id: string;
  label: string;
  latitude: string;
  longitude: string;
  setLatitude: (value: string) => void;
  setLongitude: (value: string) => void;
}) {
  return (
    <fieldset className="coordinate-fields">
      <legend>{label}</legend>
      <label htmlFor={`${id}-latitude`}>Latitude</label>
      <input
        id={`${id}-latitude`}
        type="number"
        step="any"
        min="-90"
        max="90"
        required
        value={latitude}
        onChange={(event) => setLatitude(event.target.value)}
      />
      <label htmlFor={`${id}-longitude`}>Longitude</label>
      <input
        id={`${id}-longitude`}
        type="number"
        step="any"
        min="-180"
        max="180"
        required
        value={longitude}
        onChange={(event) => setLongitude(event.target.value)}
      />
    </fieldset>
  );
}

function RideList({
  rides,
  loading,
  canCancel,
  busy,
  onCancel,
  onRefresh,
  onAdvance,
  payments = [],
  onPayment,
}: {
  rides: Ride[];
  loading: boolean;
  canCancel: boolean;
  busy: boolean;
  onCancel: (ride: Ride) => void;
  onRefresh: () => void;
  onAdvance?: (ride: Ride, action: "arriving" | "arrived" | "start" | "complete") => void;
  payments?: Payment[];
  onPayment?: (payment: Payment) => void;
}) {
  return (
    <section className="dashboard-panel ride-history-panel">
      <div className="panel-title-row">
        <div>
          <h3>{canCancel ? "Your ride history" : "Assigned rides"}</h3>
          <p className="panel-hint">Recent rides and their current state.</p>
        </div>
        <button className="text-button" disabled={busy} onClick={onRefresh} type="button">
          Refresh
        </button>
      </div>
      {loading ? (
        <p className="empty-state">Loading rides…</p>
      ) : rides.length === 0 ? (
        <p className="empty-state">No rides to show yet.</p>
      ) : (
        <div className="ride-list">
          {rides.map((ride) => (
            <article className="ride-card" key={ride.id}>
              <RideSummary ride={ride} />
              {canCancel && ride.driver_id && (ride.driver_name || ride.driver_vehicle) && (
                <p className="driver-detail">
                  Driver: {ride.driver_name ?? "Assigned"}
                  {ride.driver_vehicle ? ` · ${ride.driver_vehicle}` : ""}
                </p>
              )}
              {canCancel && (() => {
                const payment = payments.find((item) => item.ride_id === ride.id);
                if (!payment) return null;
                const payable = payment.status === "PENDING" || payment.status === "FAILED";
                return (
                  <div className="payment-summary">
                    <p>Payment: <strong>{statusLabel(payment.status)}</strong> · receipt ref {payment.reference_id}</p>
                    {payment.status === "FAILED" && payment.last_error && (
                      <p className="payment-error">Last attempt: {statusLabel(payment.last_error)}</p>
                    )}
                    {payable && payment.attempt_count < 3 && onPayment && (
                      <button
                        className="button button-secondary"
                        disabled={busy}
                        onClick={() => onPayment(payment)}
                        type="button"
                      >
                        {payment.status === "FAILED" ? "Retry local mock payment" : "Pay with local mock"}
                      </button>
                    )}
                  </div>
                );
              })()}
              {canCancel && cancelStatuses.has(ride.status) && (
                <button className="text-button danger-button" disabled={busy} onClick={() => onCancel(ride)} type="button">
                  Cancel ride
                </button>
              )}
              {onAdvance && (
                <div className="ride-actions">
                  {ride.status === "ACCEPTED" && (
                    <button className="button button-primary" disabled={busy} onClick={() => onAdvance(ride, "arriving")} type="button">Start arrival</button>
                  )}
                  {ride.status === "DRIVER_ARRIVING" && (
                    <button className="button button-primary" disabled={busy} onClick={() => onAdvance(ride, "arrived")} type="button">Mark arrived</button>
                  )}
                  {ride.status === "DRIVER_ARRIVED" && (
                    <button className="button button-primary" disabled={busy} onClick={() => onAdvance(ride, "start")} type="button">Start trip</button>
                  )}
                  {ride.status === "IN_PROGRESS" && (
                    <button className="button button-primary" disabled={busy} onClick={() => onAdvance(ride, "complete")} type="button">Complete trip</button>
                  )}
                </div>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function PaymentHistory({ payments, loading }: { payments: Payment[]; loading: boolean }) {
  return (
    <section className="dashboard-panel ride-history-panel">
      <h3>Payment history</h3>
      <p className="panel-hint">Local mock transactions only; no card details are collected.</p>
      {loading ? (
        <p className="empty-state">Loading payment history…</p>
      ) : payments.length === 0 ? (
        <p className="empty-state">No payments have been created.</p>
      ) : (
        <div className="ride-list">
          {payments.map((payment) => (
            <article className="ride-card" key={payment.id}>
              <div className="ride-card-heading">
                <span className={`status-pill status-${payment.status.toLowerCase()}`}>
                  {statusLabel(payment.status)}
                </span>
                <strong>{dollars(payment.amount_paise)}</strong>
              </div>
              <p className="coordinate-line">Ride {payment.ride_id}</p>
              <p className="ride-date">Reference {payment.reference_id} · {new Date(payment.created_at).toLocaleString()}</p>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function AdminDashboard({
  loading,
  busy,
  summary,
  users,
  drivers,
  rides,
  payments,
  reason,
  setReason,
  currentUserId,
  onVerify,
  onActivation,
  onCancelRide,
  onRefresh,
}: {
  loading: boolean;
  busy: boolean;
  summary: AdminDashboardData | null;
  users: AdminUser[];
  drivers: AdminDriver[];
  rides: Ride[];
  payments: Payment[];
  reason: string;
  setReason: (value: string) => void;
  currentUserId: string;
  onVerify: (driver: AdminDriver, verified: boolean) => void;
  onActivation: (target: AdminUser, isActive: boolean) => void;
  onCancelRide: (ride: Ride) => void;
  onRefresh: () => void;
}) {
  return (
    <>
      <section className="dashboard-panel admin-panel">
        <div className="panel-title-row">
          <div>
            <h3>Platform overview</h3>
            <p className="panel-hint">Read-only totals; payment and fare figures are INR paise.</p>
          </div>
          <button className="text-button" disabled={busy} onClick={onRefresh} type="button">Refresh</button>
        </div>
        {loading || !summary ? (
          <p className="empty-state">Loading administrator summary…</p>
        ) : (
          <div className="summary-metrics">
            <div><span>Users</span><strong>{Object.values(summary.users_by_role).reduce((total, count) => total + count, 0)}</strong></div>
            <div><span>Drivers</span><strong>{summary.drivers_total}</strong></div>
            <div><span>Completed fare total</span><strong>{dollars(summary.completed_fare_paise)}</strong></div>
            <div><span>Payments succeeded</span><strong>{summary.payments_by_status.SUCCEEDED ?? 0}</strong></div>
            <p className="panel-hint">
              Rides: {Object.entries(summary.rides_by_status).map(([key, value]) => `${statusLabel(key)} ${value}`).join(" · ") || "none"}
            </p>
          </div>
        )}
      </section>

      <section className="dashboard-panel admin-panel">
        <h3>Audited admin actions</h3>
        <label className="admin-reason-label" htmlFor="admin-reason">Reason recorded with administrative changes</label>
        <input
          id="admin-reason"
          className="admin-reason-input"
          maxLength={500}
          required
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="Provide a concise reason"
        />
      </section>

      <section className="dashboard-panel admin-panel">
        <h3>Driver management</h3>
        {loading ? <p className="empty-state">Loading drivers…</p> : drivers.length === 0 ? (
          <p className="empty-state">No driver accounts.</p>
        ) : (
          <div className="admin-record-list">
            {drivers.map((driver) => (
              <article className="ride-card admin-record" key={driver.id}>
                <strong>{driver.full_name}</strong>
                <p className="coordinate-line">{driver.email}</p>
                <p className="ride-date">
                  {statusLabel(driver.verification_status)} · {statusLabel(driver.availability)} · {driver.is_online ? "Online" : "Offline"} · {driver.is_active ? "Active account" : "Inactive account"}
                </p>
                <button
                  className="text-button"
                  disabled={busy}
                  onClick={() => onVerify(driver, driver.verification_status !== "verified")}
                  type="button"
                >
                  {driver.verification_status === "verified" ? "Reject driver" : "Verify driver"}
                </button>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="dashboard-panel admin-panel">
        <h3>User inspection</h3>
        {loading ? <p className="empty-state">Loading users…</p> : users.length === 0 ? (
          <p className="empty-state">No users.</p>
        ) : (
          <div className="admin-record-list">
            {users.map((item) => (
              <article className="ride-card admin-record" key={item.id}>
                <strong>{item.full_name}</strong>
                <p className="coordinate-line">{item.email}</p>
                <p className="ride-date">{statusLabel(item.role)} · {item.is_active ? "Active" : "Inactive"} · {new Date(item.created_at).toLocaleDateString()}</p>
                {item.role !== "admin" && item.id !== currentUserId && (
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() => onActivation(item, !item.is_active)}
                    type="button"
                  >
                    {item.is_active ? "Deactivate account" : "Reactivate account"}
                  </button>
                )}
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="dashboard-panel admin-panel">
        <h3>Ride inspection</h3>
        {loading ? <p className="empty-state">Loading rides…</p> : rides.length === 0 ? (
          <p className="empty-state">No rides.</p>
        ) : (
          <div className="ride-list">
            {rides.map((ride) => (
              <article className="ride-card" key={ride.id}>
                <RideSummary ride={ride} />
                {cancelStatuses.has(ride.status) || activeStatuses.has(ride.status) ? (
                  <button
                    className="text-button danger-button"
                    disabled={busy}
                    onClick={() => onCancelRide(ride)}
                    type="button"
                  >
                    {ride.status === "IN_PROGRESS" ? "Cancel in-progress ride" : "Cancel ride"}
                  </button>
                ) : null}
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="dashboard-panel admin-panel">
        <h3>Payment inspection</h3>
        <p className="panel-hint">Payment references and states only; no payment credentials are stored.</p>
        {loading ? <p className="empty-state">Loading payments…</p> : payments.length === 0 ? (
          <p className="empty-state">No payments.</p>
        ) : (
          <div className="ride-list">
            {payments.map((payment) => (
              <article className="ride-card" key={payment.id}>
                <div className="ride-card-heading">
                  <span className={`status-pill status-${payment.status.toLowerCase()}`}>{statusLabel(payment.status)}</span>
                  <strong>{dollars(payment.amount_paise)}</strong>
                </div>
                <p className="coordinate-line">Ride {payment.ride_id}</p>
                <p className="ride-date">Rider {payment.user_id} · {payment.reference_id} · attempts {payment.attempt_count}</p>
              </article>
            ))}
          </div>
        )}
      </section>
    </>
  );
}

function RideSummary({ ride }: { ride: Ride }) {
  return (
    <>
      <div className="ride-card-heading">
        <span className={`status-pill status-${ride.status.toLowerCase()}`}>{statusLabel(ride.status)}</span>
        <strong>{dollars(ride.fare_paise)}</strong>
      </div>
      <p className="coordinate-line">
        Pickup: {ride.pickup.latitude.toFixed(5)}, {ride.pickup.longitude.toFixed(5)}
      </p>
      <p className="coordinate-line">
        Drop-off: {ride.dropoff.latitude.toFixed(5)}, {ride.dropoff.longitude.toFixed(5)}
      </p>
      <p className="ride-date">
        {(ride.distance_meters / 1000).toFixed(2)} km straight-line · {new Date(ride.created_at).toLocaleString()}
      </p>
    </>
  );
}
