import { NextResponse } from "next/server";
import { getFirebaseDb } from "../../lib/firebaseServer";

export async function GET() {
  try {
    const db = getFirebaseDb();
    const snapshot = await db.collection("menu").limit(20).get();

    const items = snapshot.docs.map((doc) => ({
      id: doc.id,
      ...doc.data(),
    }));

    return NextResponse.json({ items });
  } catch (error) {
    console.error("Menu API error:", error);
    return NextResponse.json(
      {
        items: [],
        error: "Failed to load menu from Firestore",
      },
      { status: 500 }
    );
  }
}
