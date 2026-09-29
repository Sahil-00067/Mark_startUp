import { NextResponse } from "next/server";
import { getFirebaseDb } from "../../lib/firebaseServer";

export async function POST(request: Request) {
  try {
    const payload = await request.json();
    const db = getFirebaseDb();
    const doc = await db.collection("orders").add({
      ...payload,
      createdAt: new Date().toISOString(),
    });

    return NextResponse.json({ ok: true, id: doc.id });
  } catch (error) {
    console.error("Orders API error:", error);
    return NextResponse.json(
      {
        ok: false,
        error: "Failed to create order in Firestore",
      },
      { status: 500 }
    );
  }
}
